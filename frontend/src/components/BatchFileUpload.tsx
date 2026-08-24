/**
 * 批量文件上传 + 一键入库组件（2026-08 替代 SingleFileUpload）。
 *
 * 用法：
 *   - 用户拖拽或点击选择多个文件（最多 50 个）
 *   - 选 auto_ingest 开关：上传后自动跑 parse+chunk+dify 全流程
 *   - 点"上传并入库"按钮
 *   - 上传完成后展示：每个文件的状态 + 各阶段统计
 *
 * 业务价值：
 *   - 不需要先在 manifest.xlsx 加行
 *   - 一次选多个文件批量入库，1 个失败不影响其他
 *   - 测试完成后 Excel 留有记录可追溯
 *
 * ★ 2026-08 改造点（与 SingleFileUpload 对比）：
 *   - 接受多个文件（antd Dragger `multiple=true`, `maxCount=50`）
 *   - 文件列表实时展示（antd `showUploadList`）
 *   - 后端走 /api/upload/batch 端点，一次 run_pipeline 跑所有文件
 *   - 结果按文件列表渲染，每行展示 per-file 状态
 *   - 标题改为"批量文件上传 + 一键入库"
 */

import {
  Alert,
  Badge,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Progress,
  Row,
  Space,
  Statistic,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
  Upload,
  message,
} from "antd";
import {
  CloudUploadOutlined,
  InboxOutlined,
  RocketOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  FileTextOutlined,
  WarningOutlined,
  MinusCircleOutlined,
} from "@ant-design/icons";
import { useState } from "react";
import type { UploadFile, UploadProps } from "antd";
import {
  uploadBatchFiles,
  getParseProgress,
  type BatchItemResponse,
  type BatchUploadResponse,
  type PipelineStatus,
  type ParseProgressMap,
} from "../api/client";

const { Text, Paragraph } = Typography;
const { Dragger } = Upload;

const ACCEPT_EXTS = [".pdf", ".docx", ".doc", ".pptx", ".xlsx"];
const MAX_BATCH_COUNT = 600;

interface Props {
  onAfterUpload?: (r: BatchUploadResponse) => void;
  loading?: boolean;
  onLoadingChange?: (v: boolean) => void;
}

const STATUS_COLORS: Record<PipelineStatus, string> = {
  ok: "green",
  partial: "gold",
  failed: "red",
  skipped: "default",
  pending: "blue",
};

const STATUS_LABELS: Record<PipelineStatus, string> = {
  ok: "全部成功",
  partial: "部分成功",
  failed: "全部失败",
  skipped: "全部跳过",
  pending: "执行中…",
};

const STAGE_COLORS: Record<string, string> = {
  // parse actions
  parsed: "green",
  skipped_parsed: "default",
  parse_failed: "red",
  dry_run_parse: "blue",
  no_pending: "default",
  // chunk actions
  chunked: "green",
  skipped_chunked: "default",
  chunk_failed: "red",
  dry_run_chunk: "blue",
  no_parsed: "default",
  // dify actions
  uploaded: "green",
  failed: "red",
  dry_run: "blue",
};

const STAGE_LABELS: Record<string, string> = {
  parsed: "解析成功",
  skipped_parsed: "已解析",
  parse_failed: "解析失败",
  chunked: "切分成功",
  skipped_chunked: "已切分",
  chunk_failed: "切分失败",
  uploaded: "入库成功",
  failed: "入库失败",
};

export default function BatchFileUpload({
  onAfterUpload,
  loading = false,
  onLoadingChange,
}: Props) {
  const [fileList, setFileList] = useState<UploadFile[]>([]);
  const [autoIngest, setAutoIngest] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [result, setResult] = useState<BatchUploadResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [msgApi, contextHolder] = message.useMessage();
  /** ★ 2026-08-07：MinerU 解析进度（实时轮询） */
  const [parseProgressMap, setParseProgressMap] = useState<ParseProgressMap>({});

  // 内部 loading（暴露给外部时合并）
  const isLoading = loading || uploading;
  const setIsLoading = (v: boolean) => {
    setUploading(v);
    onLoadingChange?.(v);
  };

  const handleUpload = async () => {
    if (fileList.length === 0) {
      msgApi.warning("请先选择文件");
      return;
    }
    // 拿到原生 File 对象（originFileObj 是 RcFile，RcFile 继承自 File 实际就是 File，
    //  但 TS 类型上多了 uid/lastModifiedDate，用 unknown 中转避免结构化类型不兼容）
    const rawFiles: File[] = fileList
      .map((f) => f.originFileObj)
      .filter((f): f is NonNullable<typeof f> => Boolean(f)) as unknown as File[];
    if (rawFiles.length === 0) {
      msgApi.warning("所选文件无效");
      return;
    }
    setIsLoading(true);
    setError(null);
    setParseProgressMap({}); // ★ 2026-08-07：重置进度
    try {
      // ★ 2026-08-07：启动轮询进度（每 1 秒查询一次）
      const progressInterval = setInterval(async () => {
        try {
          const progress = await getParseProgress();
          setParseProgressMap(progress);
        } catch (e) {
          console.warn("查询解析进度失败:", e);
        }
      }, 1000);

      const resp = await uploadBatchFiles(rawFiles, autoIngest);
      clearInterval(progressInterval);
      
      // ★ 2026-08-07：最后一次查询进度（确保拿到最终状态）
      try {
        const finalProgress = await getParseProgress();
        setParseProgressMap(finalProgress);
      } catch (e) {
        console.warn("查询最终解析进度失败:", e);
      }
      
      setResult(resp);
      onAfterUpload?.(resp);
      // 整体汇总提示
      if (resp.failed === 0 && resp.pipeline?.status === "ok") {
        msgApi.success(
          `✅ 批量上传入库成功：${resp.succeeded}/${resp.total} 个文件`
        );
      } else if (resp.failed > 0 && resp.succeeded === 0) {
        msgApi.error(`❌ 全部 ${resp.total} 个文件保存失败，查看详情`);
      } else if (resp.failed > 0) {
        msgApi.warning(
          `⚠️ 部分失败：成功 ${resp.succeeded}，失败 ${resp.failed}，共 ${resp.total}`
        );
      } else if (resp.pipeline?.status === "partial") {
        msgApi.warning(
          `⚠️ 全部上传成功，但流水线部分阶段异常`
        );
      } else {
        msgApi.success(
          `✅ 批量上传成功：${resp.succeeded}/${resp.total} 个文件（未触发入库）`
        );
      }
    } catch (e) {
      const msg = (e as Error).message;
      setError(msg);
      msgApi.error(`批量上传失败：${msg}`);
    } finally {
      setIsLoading(false);
      // ★ 2026-08-07：10 秒后清空进度条
      setTimeout(() => setParseProgressMap({}), 10000);
    }
  };

  // antd Upload props（自定义上传逻辑，关闭默认 action）
  //   - beforeUpload 返回 false 阻止 antd 自动上传
  //   - onChange 统一接管 fileList（antd v5 在 beforeUpload 拦截后仍会触发 onChange）
  //   - maxCount 限制最多 600 个
  const uploadProps: UploadProps = {
    multiple: true,
    maxCount: MAX_BATCH_COUNT,
    accept: ACCEPT_EXTS.join(","),
    fileList,
    beforeUpload: () => false,
    onChange: ({ fileList: fl }) => {
      // 限流：超过 MAX_BATCH_COUNT 的部分丢弃最早加入的
      const capped = fl.slice(-MAX_BATCH_COUNT);
      setFileList(capped);
      setResult(null);
      setError(null);
    },
    onRemove: (file) => {
      setFileList((prev) => prev.filter((f) => f.uid !== file.uid));
    },
  };

  // 表格列定义
  const itemColumns = [
    {
      title: "文件名",
      dataIndex: "filename",
      key: "filename",
      render: (name: string, row: BatchItemResponse) => (
        <Space direction="vertical" size={0}>
          <Space>
            <FileTextOutlined />
            <Text strong style={{ wordBreak: "break-all" }}>
              {name || row.filename}
            </Text>
          </Space>
          {row.size > 0 && (
            <Text type="secondary" style={{ fontSize: 11 }}>
              {(row.size / 1024).toFixed(1)} KB · MD5 {row.md5?.slice(0, 8)}…
            </Text>
          )}
        </Space>
      ),
    },
    {
      title: "保存",
      dataIndex: "manifest_row_added",
      key: "manifest_row_added",
      width: 80,
      render: (ok: boolean) =>
        ok ? (
          <Tag color="green" icon={<CheckCircleOutlined />}>
            已保存
          </Tag>
        ) : (
          <Tag color="default" icon={<MinusCircleOutlined />}>
            -
          </Tag>
        ),
    },
    {
      title: "解析",
      key: "parse",
      width: 150,
      render: (_: unknown, row: BatchItemResponse) => {
        if (row.error && !row.pipeline) return <Tag color="red">失败</Tag>;
        const a = row.pipeline?.parse;
        if (!a) return <Tag color="default">-</Tag>;
        
        // ★ 2026-08-07：展示解析进度条
        const isParsing = a.action === "parsed" || a.action === "parse_failed";
        const progress = a.progress ?? 0;
        const progressMsg = a.progress_msg || "";
        
        return (
          <Space direction="vertical" size={0} style={{ width: "100%" }}>
            <Tag color={STAGE_COLORS[a.action] || "default"}>
              {STAGE_LABELS[a.action] || a.action}
            </Tag>
            {isParsing && progress > 0 && progress < 100 && (
              <Progress
                percent={progress}
                size="small"
                status="active"
                style={{ width: 120, marginTop: 2 }}
              />
            )}
            {progressMsg && (
              <Text type="secondary" style={{ fontSize: 11 }}>
                {progressMsg}
              </Text>
            )}
            {a.duration_ms != null && (
              <Text type="secondary" style={{ fontSize: 11 }}>
                {(a.duration_ms / 1000).toFixed(1)}s
                {a.attempts != null && a.attempts > 1 && ` · ${a.attempts} 次重试`}
              </Text>
            )}
          </Space>
        );
      },
    },
    {
      title: "切分",
      key: "chunk",
      width: 110,
      render: (_: unknown, row: BatchItemResponse) => {
        const a = row.pipeline?.chunk;
        if (!a) return <Tag color="default">-</Tag>;
        return (
          <Space direction="vertical" size={0}>
            <Tag color={STAGE_COLORS[a.action] || "default"}>
              {STAGE_LABELS[a.action] || a.action}
            </Tag>
            {a.chunk_count != null && (
              <Text type="secondary" style={{ fontSize: 11 }}>
                {a.chunk_count} chunks · {a.total_chars?.toLocaleString()} 字
              </Text>
            )}
          </Space>
        );
      },
    },
    {
      title: "入库",
      key: "dify",
      width: 110,
      render: (_: unknown, row: BatchItemResponse) => {
        const a = row.pipeline?.dify;
        if (!a) return <Tag color="default">-</Tag>;
        return (
          <Tag color={STAGE_COLORS[a.action] || "default"}>
            {STAGE_LABELS[a.action] || a.action}
          </Tag>
        );
      },
    },
    {
      title: "错误",
      dataIndex: "error",
      key: "error",
      render: (err: string | null) =>
        err ? (
          <Tooltip title={err}>
            <Tag color="red" icon={<CloseCircleOutlined />}>
              失败
            </Tag>
          </Tooltip>
        ) : (
          <Tag color="default">-</Tag>
        ),
    },
  ];

  return (
    <Card
      title={
        <Space>
          <CloudUploadOutlined style={{ color: "#1677ff" }} />
          <span>批量文件上传 + 一键入库（§3.x 升级）</span>
          <Tag color="blue">无需 Excel</Tag>
          <Tag color="cyan">最多 {MAX_BATCH_COUNT} 个</Tag>
        </Space>
      }
      extra={
        <Space>
          <Tooltip title="上传后自动触发 parse + chunk + dify 全流程；关闭则只上传到 pending/，不自动入库">
            <Space>
              <RocketOutlined />
              <Text>上传后自动入库</Text>
              <Switch
                checked={autoIngest}
                onChange={setAutoIngest}
                disabled={isLoading}
              />
            </Space>
          </Tooltip>
          <Button
            type="primary"
            icon={<CloudUploadOutlined />}
            loading={isLoading}
            onClick={handleUpload}
            disabled={fileList.length === 0}
          >
            {autoIngest
              ? `上传并入库（${fileList.length}）`
              : `仅上传（${fileList.length}）`}
          </Button>
        </Space>
      }
    >
      {contextHolder}
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {/* 拖拽上传区 */}
        <Dragger {...uploadProps} style={{ padding: 8 }}>
          <p className="ant-upload-drag-icon">
            <InboxOutlined style={{ color: "#1677ff" }} />
          </p>
          <p className="ant-upload-text" style={{ fontSize: 14 }}>
            {fileList.length > 0
              ? `已选 ${fileList.length} 个文件`
              : "点击或拖拽多个文件到此区域上传"}
          </p>
          <p className="ant-upload-hint" style={{ fontSize: 12 }}>
            支持格式：PDF / DOCX / DOC / PPTX / XLSX（一次最多 {MAX_BATCH_COUNT} 个）
            <br />
            上传后自动在 manifest.xlsx 追加行，并移动到 pending/ 等待处理
            <br />
            <Text type="secondary">
              1 个文件失败不影响其他文件，可与混合格式 / 混合大小一起提交
            </Text>
          </p>
        </Dragger>

        {/* ★ 2026-08-07：MinerU 解析进度条（实时轮询） */}
        {isLoading && Object.keys(parseProgressMap).length > 0 && (
          <Card size="small" type="inner" style={{ backgroundColor: "#f6ffed" }}>
            <Space direction="vertical" size="small" style={{ width: "100%" }}>
              <Space>
                <RocketOutlined style={{ color: "#1677ff" }} />
                <Text strong>MinerU 解析中...</Text>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  正在处理 {Object.keys(parseProgressMap).length} 个文件
                </Text>
              </Space>
              {Object.entries(parseProgressMap).map(([filename, item]) => (
                <div key={filename} style={{ width: "100%" }}>
                  <Space direction="vertical" size={0} style={{ width: "100%" }}>
                    <Space style={{ width: "100%", justifyContent: "space-between" }}>
                      <Text style={{ fontSize: 12, maxWidth: 400, wordBreak: "break-all" }}>
                        {filename}
                      </Text>
                      <Text type="secondary" style={{ fontSize: 11 }}>
                        {item.msg}
                      </Text>
                    </Space>
                    <Progress
                      percent={item.progress}
                      size="small"
                      status={
                        item.status === "done"
                          ? "success"
                          : item.status === "failed"
                            ? "exception"
                            : "active"
                      }
                      strokeColor={{
                        "0%": "#108ee9",
                        "100%": "#87d068",
                      }}
                    />
                  </Space>
                </div>
              ))}
            </Space>
          </Card>
        )}

        {/* 错误信息 */}
        {error && (
          <Alert
            type="error"
            showIcon
            message={
              <Space>
                <CloseCircleOutlined />
                <Text>上传失败</Text>
              </Space>
            }
            description={<code style={{ fontSize: 12 }}>{error}</code>}
          />
        )}

        {/* 整批结果概览 */}
        {result && (
          <Card
            size="small"
            type="inner"
            title={
              <Space>
                <RocketOutlined />
                <span>批量上传概览</span>
                <Tag color={result.failed > 0 ? "gold" : "green"}>
                  成功 {result.succeeded} / 失败 {result.failed} / 共 {result.total}
                </Tag>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  耗时 {(result.duration_ms / 1000).toFixed(1)}s
                </Text>
              </Space>
            }
          >
            <Row gutter={16}>
              <Col xs={12} md={6}>
                <Statistic title="总文件数" value={result.total} suffix="个" />
              </Col>
              <Col xs={12} md={6}>
                <Statistic
                  title="成功保存"
                  value={result.succeeded}
                  suffix="个"
                  valueStyle={{ color: "#52c41a" }}
                />
              </Col>
              <Col xs={12} md={6}>
                <Statistic
                  title="失败"
                  value={result.failed}
                  suffix="个"
                  valueStyle={{ color: result.failed > 0 ? "#ff4d4f" : undefined }}
                />
              </Col>
              <Col xs={12} md={6}>
                <Statistic
                  title="整批流水线"
                  value={result.pipeline?.status || "-"}
                  valueStyle={{
                    fontSize: 14,
                    color:
                      result.pipeline?.status === "ok"
                        ? "#52c41a"
                        : result.pipeline?.status === "partial"
                          ? "#faad14"
                          : result.pipeline?.status === "failed"
                            ? "#ff4d4f"
                            : undefined,
                  }}
                />
              </Col>
            </Row>
          </Card>
        )}

        {/* 每个文件的结果列表 */}
        {result && result.items && result.items.length > 0 && (
          <Card size="small" type="inner" title="📋 每个文件的结果">
            <Table<BatchItemResponse>
              size="small"
              rowKey={(r) => `${r.filename}-${r.saved_path}`}
              dataSource={result.items}
              columns={itemColumns as any}
              pagination={false}
              scroll={{ x: 720 }}
            />
          </Card>
        )}

        {!result && fileList.length === 0 && (
          <Empty
            description={
              <Text type="secondary">还没有选择文件，拖拽或点击上传区域开始</Text>
            }
          />
        )}

        <Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 0 }}>
          <Badge color="blue" text="用法说明" />
          ：选文件（可多选） → 选「自动入库」开关 → 点「上传」。
          若开启自动入库，全部文件上传后会自动跑一次 parse+chunk+dify 流水线；
          1 个文件失败不会影响其他文件。
          测试文件可在 <code>data/pending/</code> 或{" "}
          <code>data/manifest.xlsx</code> 找到。
        </Paragraph>
      </Space>
    </Card>
  );
}
