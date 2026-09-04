/**
 * 网页抓取「下载后的文件预览」抽屉（2026-09 新增）。
 *
 * 用途：confirm（确认下载）落地到 pending/ 后，在这里直接预览下载到的真实文件
 * （PDF/HTML/图片/txt/md 直接看；Word/Excel/PPT/CSV 由后端轻量转换后在线看，
 * .doc/.xls/.ppt 旧版 Office 由后端经 LibreOffice 转换后同样在线看；压缩包等
 * 无法转换的二进制给文件信息 + 下载自查），预览处点「确定并解析入库」才对该项
 * 走解析 → 切分 → Dify 入库（仅当前这一项）。
 */
import {
  Alert,
  Button,
  Descriptions,
  Drawer,
  Empty,
  Progress,
  Result,
  Space,
  Spin,
  Tabs,
  Tag,
  Typography,
} from "antd";
import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  fetchWebScrapeText,
  getParseProgress,
  ingestWebScrapeItem,
  previewWebScrapeItem,
  webScrapeFileUrl,
  webScrapeOfficeUrl,
  webScrapePreviewKind,
  type ParseProgressItem,
  type WebScrapeIngestItemResult,
  type WebScrapeItem,
  type WebScrapeTask,
} from "../api/client";
import MarkdownPreview from "./MarkdownPreview";

const { Text } = Typography;

interface Props {
  taskId: string;
  /** 当前预览的任务项（父组件从最新 task 派生，入库后自动刷新到最新状态） */
  item: WebScrapeItem | null;
  /** 该项在 task.items 中的下标（文件预览接口按 index 定位） */
  index: number;
  open: boolean;
  onClose: () => void;
  /** 入库成功/失败后父组件用它拿到最新 task 并刷新历史/台账 */
  onTaskUpdated?: (task: WebScrapeTask) => void;
}

function formatBytes(v?: number | null): string {
  if (!v) return "-";
  if (v > 1024 * 1024) return `${(v / 1024 / 1024).toFixed(1)}MB`;
  if (v > 1024) return `${(v / 1024).toFixed(1)}KB`;
  return `${v}B`;
}

export default function WebScrapeFilePreview({
  taskId,
  item,
  index,
  open,
  onClose,
  onTaskUpdated,
}: Props) {
  const [mdLoading, setMdLoading] = useState(false);
  const [mdError, setMdError] = useState<string | null>(null);
  const [mdContent, setMdContent] = useState<string | null>(null);
  const [textLoading, setTextLoading] = useState(false);
  const [text, setText] = useState<string | null>(null);
  const [textError, setTextError] = useState<string | null>(null);

  const [ingesting, setIngesting] = useState(false);
  const [ingestResult, setIngestResult] = useState<WebScrapeIngestItemResult | null>(null);
  const [parseProgress, setParseProgress] = useState<Record<string, ParseProgressItem>>({});
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const filename = item?.filename ?? "";
  const kind = webScrapePreviewKind(filename);
  const fileUrl = webScrapeFileUrl(taskId, index);
  const officeUrl = webScrapeOfficeUrl(taskId, index);
  const isContent = item?.kind === "content";
  // downloaded=待预览确认 / error=失败可重试 → 显示「确定并解析入库」（需有落地文件）
  const canIngest = !!item?.confirmed && !!filename && item.ingest_status !== "ok";
  const canPreviewMd = isContent && !!item?.rel_path;

  useEffect(() => {
    if (!open) return;
    setIngestResult(null);
    setParseProgress({});
    // 网页正文：预取 Markdown 内容（原抓取内容，用于和渲染后的 PDF 对照确认）
    if (canPreviewMd) {
      setMdLoading(true);
      setMdError(null);
      previewWebScrapeItem(taskId, index)
        .then((p) => setMdContent(p.content ?? ""))
        .catch((e) => setMdError(String(e)))
        .finally(() => setMdLoading(false));
    }
    // txt/md 类附件：读取文件文本在线展示
    if (!isContent && (kind === "markdown" || kind === "text")) {
      setTextLoading(true);
      setTextError(null);
      fetchWebScrapeText(taskId, index)
        .then(setText)
        .catch((e) => {
          setTextError(String(e));
          setText(null);
        })
        .finally(() => setTextLoading(false));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, taskId, index, item?.url]);

  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
      pollRef.current = null;
    };
  }, []);

  const clearPoll = () => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };

  // 预览处点「确定」→ 仅当前预览项走 解析 → 切分 → Dify 入库
  const handleIngest = async () => {
    if (!item || !canIngest || ingesting) return;
    setIngesting(true);
    setIngestResult(null);
    // 解析期间轮询 MinerU 进度（key=落地文件名，与后端 parser 一致）
    const targetName = filename;
    const startPoll = () => {
      clearPoll();
      pollRef.current = setInterval(() => {
        getParseProgress()
          .then((m) => {
            if (targetName && m[targetName]) {
              setParseProgress((prev) => ({
                ...prev,
                [targetName]: m[targetName],
              }));
            }
          })
          .catch(() => undefined);
      }, 1200);
    };
    startPoll();
    try {
      const resp = await ingestWebScrapeItem(taskId, [item.url]);
      clearPoll();
      const r = resp.results.find((x) => x.url === item.url) ?? null;
      setIngestResult(r);
      if (resp.task) onTaskUpdated?.(resp.task);
    } catch (e) {
      clearPoll();
      setIngestResult({
        url: item.url,
        ok: false,
        status: "error",
        error: String(e),
      });
    } finally {
      clearPoll();
      setIngesting(false);
      setParseProgress({});
    }
  };

  const previewBody = (): ReactNode => {
    if (!item) return <Empty description="无任务项信息" />;
    if (!item.confirmed) {
      return (
        <Alert
          type="warning"
          showIcon
          message="该项尚未确认下载"
          description="请先回到列表点击「确认下载」，落地到待处理区后才能预览下载到的文件。"
        />
      );
    }
    const mdView = (
      <div style={{ maxHeight: "calc(100vh - 300px)", overflow: "auto" }}>
        {mdLoading ? (
          <div style={{ textAlign: "center", padding: 48 }}>
            <Spin />
          </div>
        ) : mdError ? (
          <Alert type="warning" showIcon message={`正文加载失败：${mdError}`} />
        ) : mdContent ? (
          <MarkdownPreview content={mdContent} />
        ) : (
          <Empty description="正文内容为空" />
        )}
      </div>
    );

    if (!filename) {
      // 旧版本确认入库的记录没存落地文件名：正文仍可看抓取的 Markdown 原文
      if (isContent && canPreviewMd) {
        return (
          <>
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 12 }}
              message="该项来自旧版流程（正文已直接入库），未记录落地文件；以下为抓取的正文原文。"
            />
            {mdView}
          </>
        );
      }
      return (
        <Alert
          type="warning"
          showIcon
          message="找不到可预览的落地文件"
          description="该项未记录落地文件名（可能为旧版本确认入库的记录，已直接入库）。"
        />
      );
    }

    // 网页正文：落地文件（将解析的对象）+ 抓取 Markdown 原文 双视图（默认看文件）
    if (isContent && canPreviewMd) {
      return (
        <Tabs
          defaultActiveKey="file"
          items={[
            {
              key: "file",
              label: `落地文件（${filename.replace(/^.*\./, "")}，解析对象）`,
              children: renderFileBody(),
            },
            {
              key: "md",
              label: "正文内容（Markdown，抓取原文）",
              children: mdView,
            },
          ]}
        />
      );
    }

    return renderFileBody();
  };

  const renderFileBody = (): ReactNode => {
    if (kind === "pdf" || kind === "html") {
      return (
        <iframe
          key={`${kind}-${filename}`}
          src={fileUrl}
          title={filename}
          style={{
            width: "100%",
            height: "calc(100vh - 300px)",
            border: "1px solid #e5e7eb",
            borderRadius: 6,
            background: "#fff",
          }}
        />
      );
    }
    if (kind === "image") {
      return (
        <div style={{ textAlign: "center", background: "#f8fafc", padding: 16, borderRadius: 6 }}>
          <img src={fileUrl} alt={filename} style={{ maxWidth: "100%", maxHeight: "calc(100vh - 320px)" }} />
        </div>
      );
    }
    if (kind === "markdown" || kind === "text") {
      return (
        <Spin spinning={textLoading}>
          {textError ? (
            <Alert type="warning" showIcon message={`文本读取失败：${textError}`} />
          ) : kind === "markdown" && text !== null ? (
            <div style={{ maxHeight: "calc(100vh - 300px)", overflow: "auto" }}>
              <MarkdownPreview content={text} />
            </div>
          ) : text !== null ? (
            <pre
              style={{
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
                background: "#f8fafc",
                padding: 12,
                borderRadius: 6,
                maxHeight: "calc(100vh - 300px)",
                overflow: "auto",
              }}
            >
              {text}
            </pre>
          ) : (
            <Empty description="加载中..." />
          )}
        </Spin>
      );
    }
    if (kind === "office" || kind === "csv") {
      const isLegacyOffice = /\.(doc|xls|ppt)$/i.test(filename);
      return (
        <>
          {isLegacyOffice && (
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 12 }}
              message="旧版 Office 正在经 LibreOffice 在线转换预览"
              description=".doc/.xls/.ppt 由服务器端 LibreOffice 转换后在线查看；首次打开需等待几秒，若一直空白或转换失败，可点右下角「下载原文件」查看。"
            />
          )}
          <iframe
            key={`office-${filename}`}
            src={officeUrl}
            title={filename}
            style={{
              width: "100%",
              height: "calc(100vh - 300px)",
              border: "1px solid #e5e7eb",
              borderRadius: 6,
              background: "#fff",
            }}
          />
        </>
      );
    }
    // archive / other 等后端无法转换的二进制：文件信息 + 下载自查
    const note =
      kind === "archive"
        ? "压缩包无法在线预览，请下载后用本地解压工具查看其中文件。"
        : "该格式暂不支持在线预览，请下载原文件查看。";
    return (
      <Result
        status="info"
        title={filename}
        subTitle={note}
        extra={
          <Button type="primary" href={fileUrl} target="_blank" rel="noreferrer">
            下载原文件自查
          </Button>
        }
      />
    );
  };

  const progressItem = filename ? parseProgress[filename] : undefined;
  const statusColor: Record<string, string> = {
    downloaded: "blue",
    ok: "green",
    error: "red",
  };

  return (
    <Drawer
      title={
        <Space size={8}>
          {/* 与确认下载弹窗「第 1 步 / 共 2 步」对应：确认下载后进入本抽屉即为第 2 步
              （预览核对文件 → 点「确定并解析入库」），入库完成后不再标注步骤 */}
          <span>
            文件预览
            {item?.confirmed && item.ingest_status !== "ok" ? "（第 2 步 / 共 2 步）" : ""}
            {ingesting ? " · 解析入库中" : ""}
          </span>
          {item?.confirmed && (
            <Tag color={statusColor[item.ingest_status ?? ""] ?? "default"}>
              {item.ingest_status === "downloaded"
                ? "已下载 · 待预览确认"
                : item.ingest_status === "ok"
                  ? "已入库"
                  : item.ingest_status === "error"
                    ? "入库失败"
                    : "已确认下载"}
            </Tag>
          )}
        </Space>
      }
      width={960}
      open={open}
      onClose={onClose}
      destroyOnHidden
      footer={
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <Button href={fileUrl} target="_blank" rel="noreferrer" disabled={!filename}>
            下载原文件
          </Button>
          {canIngest ? (
            <Button type="primary" loading={ingesting} onClick={handleIngest}>
              {ingesting ? "解析入库中..." : item?.ingest_status === "error" ? "重新解析入库" : "确定并解析入库"}
            </Button>
          ) : (
            <Button type="primary" onClick={onClose}>
              完成
            </Button>
          )}
        </div>
      }
    >
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        <Descriptions size="small" column={3} bordered>
          <Descriptions.Item label="类型" span={1}>
            {isContent ? <Tag color="cyan">网页内容</Tag> : <Tag color="blue">附件文件</Tag>}
          </Descriptions.Item>
          <Descriptions.Item label="文件名" span={2}>
            {filename || "-"}
          </Descriptions.Item>
          <Descriptions.Item label="规模" span={1}>
            {item
              ? isContent
                ? `${(item.size ?? 0).toLocaleString()} 字（正文）`
                : formatBytes(item.size)
              : "-"}
          </Descriptions.Item>
          <Descriptions.Item label="目标知识库" span={2}>
            {item?.dataset_name || item?.dataset_id || "-"}
          </Descriptions.Item>
        </Descriptions>

        {canIngest && item?.ingest_status === "downloaded" && !ingestResult && (
          <Alert
            type="info"
            showIcon
            message="第 2 步 · 下载完成，请确认文件内容"
            description="确认这里预览的文件正是要入库的内容后，点右下角「确定并解析入库」，将仅对当前这一项走 解析 → 切分 → Dify 入库。"
          />
        )}
        {item?.ingest_status === "error" && !ingestResult && (
          <Alert
            type="error"
            showIcon
            message={`上次入库失败：${item.ingest_error ?? "未知错误"}`}
            description="可点右下角「重新解析入库」重试（会基于已下载文件重新执行 解析 → 切分 → 入库）。"
          />
        )}
        {ingesting && progressItem && (
          <Progress
            percent={progressItem.progress ?? 0}
            status={progressItem.status === "failed" ? "exception" : "active"}
            format={() => `${progressItem.msg ?? "解析中"}`}
          />
        )}
        {ingestResult && (
          <Alert
            type={ingestResult.ok ? "success" : "error"}
            showIcon
            message={ingestResult.ok ? "解析 → 切分 → 入库 完成" : "入库失败"}
            description={
              ingestResult.error ??
              (ingestResult.dify_doc_id
                ? `Dify 文档 ID：${ingestResult.dify_doc_id}`
                : "已完成入库")
            }
          />
        )}

        <div style={{ width: "100%" }}>{previewBody()}</div>
      </Space>
    </Drawer>
  );
}
