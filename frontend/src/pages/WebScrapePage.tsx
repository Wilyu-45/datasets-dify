/**
 * 网站抓取页面（2026-08 新增：知识库内容外延，三步式确认）。
 *
 * 流程：
 *   ① 选择「网站抓取配置」（其「抓取网站 URL 列表」webscrape_urls 即抓取来源）
 *   ② 确认抓取来源并开始抓取：网页正文转 Markdown、附件文件下载，
 *      生成「待确认任务」（此阶段不入库）
 *   ③ 行上「确认下载」→ 再次选择配置 + 目标知识库 → 只把该项落地到 pending/
 *      （网页正文 → 渲染 PDF、附件 → 原文件），不触发流水线
 *   ④ 下载完成后自动打开「文件预览」：直接预览下载到的真实文件
 *      （PDF/HTML/图片/txt/md 直接看；Word/Excel/PPT/CSV 在线预览；
 *       旧版 Office/压缩包给文件信息 + 下载自查）→ 预览处点「确定并解析入库」
 *      → 仅对当前这一项走 parse(MinerU) → 切分 → Dify 入库
 *
 * ★ 2026-08-31 两套配置：URL 来自配置本身（不再手动输入）；
 *   只有「网站抓取配置」能在此页用于抓取；
 *   网站抓取配置不含知识库 ID：确认下载时每次选择目标知识库（可入不同库），
 *   同一抓取任务可分多批确认，分别入到不同知识库。
 * ★ 2026-09-02 「确认下载 → 文件预览 → 确定入库」两段式：下载后的真实文件可
 *   先预览核对（参考网页版 Office 的在线预览），预览处点确定才解析入库。
 *
 * 布局：顶部配置选择；左侧配置信息；右侧任务表格 + 预览抽屉；
 *       底部历史任务列表 + 入库台账。
 */
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Drawer,
  Empty,
  Modal,
  Radio,
  Row,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useMemo, useState } from "react";
import {
  confirmWebScrapeTask,
  getWebScrapeTask,
  listConfigProfiles,
  listDifyDatasets,
  listWebScrapeRecords,
  listWebScrapeTasks,
  previewWebScrapeItem,
  runWebScrape,
  type ConfigProfile,
  type DifyDatasetItem,
  type WebScrapeItem,
  type WebScrapePreviewResponse,
  type WebScrapeRecordItem,
  type WebScrapeTask,
  type WebScrapeTaskListItem,
} from "../api/client";
import MarkdownPreview from "../components/MarkdownPreview";
import WebScrapeFilePreview from "../components/WebScrapeFilePreview";

const { Title, Paragraph, Text } = Typography;

interface Props {
  /** 跳转配置中心 */
  onOpenConfig?: () => void;
}

const STATUS_COLOR: Record<string, string> = {
  pending: "orange",
  confirmed: "green",
  done: "green",
  cancelled: "default",
};

/** 逐项入库状态展示（downloaded=已下载待预览确认 / ok=已入库 / error=失败） */
const INGEST_STATUS_META: Record<string, { color: string; label: string }> = {
  downloaded: { color: "blue", label: "已下载" },
  ok: { color: "green", label: "已入库" },
  error: { color: "red", label: "入库失败" },
};

export default function WebScrapePage({ onOpenConfig }: Props) {
  // ---- 配置选择（先选网站抓取配置，其 URL 列表即抓取来源）----
  const [profiles, setProfiles] = useState<ConfigProfile[]>([]);
  const [selectedProfileId, setSelectedProfileId] = useState<string>();

  // ---- 抓取 ----
  const [running, setRunning] = useState(false);

  // ---- 任务与预览 ----
  const [task, setTask] = useState<WebScrapeTask | null>(null);
  const [taskLoading, setTaskLoading] = useState(false);
  const [selectedUrls, setSelectedUrls] = useState<string[]>([]);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewIdx, setPreviewIdx] = useState<number>(-1);
  const [preview, setPreview] = useState<WebScrapePreviewResponse | null>(null);

  // ---- 确认下载（第 1 步）----
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [confirmProfileId, setConfirmProfileId] = useState<string>();
  // ★ 2026-08-31 目标知识库：每次确认时选择（网站抓取配置不含知识库 ID，可入不同库）
  const [confirmDatasetId, setConfirmDatasetId] = useState<string>();
  const [datasets, setDatasets] = useState<DifyDatasetItem[]>([]);
  const [datasetsLoading, setDatasetsLoading] = useState(false);
  const [datasetsError, setDatasetsError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);

  // ---- 下载后文件预览（第 2 步：预览处点「确定」→ 逐项入库）----
  const [fileOpen, setFileOpen] = useState(false);
  const [fileIdx, setFileIdx] = useState<number>(-1);

  // ---- 历史任务 ----
  const [historyTasks, setHistoryTasks] = useState<WebScrapeTaskListItem[]>([]);

  // ---- 入库台账（webscrape_records 表，独立于文档上传的 manifest）----
  const [records, setRecords] = useState<WebScrapeRecordItem[]>([]);

  // 加载配置方案（只取网站抓取类型）+ 知识库列表
  useEffect(() => {
    loadDatasets();
    listConfigProfiles()
      .then((pr) => {
        const webProfiles = pr.profiles.filter((p) => (p.type ?? "upload") === "webscrape");
        setProfiles(webProfiles);
        // 默认选中：网站抓取类型独立激活的方案（若存在），否则第一个网站抓取配置
        const activeWebId = pr.active_profile_ids?.webscrape;
        const active = activeWebId
          ? pr.profiles.find((p) => p.id === activeWebId && (p.type ?? "upload") === "webscrape")
          : undefined;
        setSelectedProfileId(active?.id ?? webProfiles[0]?.id);
      })
      .catch(() => setProfiles([]));
    refreshHistory();
    refreshRecords();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 选中配置后展示其「抓取网站 URL 列表」
  const selectedProfile = useMemo(
    () => profiles.find((p) => p.id === selectedProfileId) ?? null,
    [profiles, selectedProfileId]
  );
  const profileUrls = useMemo(() => {
    const v = selectedProfile?.config.webscrape_urls;
    return Array.isArray(v) ? (v as string[]).map((s) => s.trim()).filter(Boolean) : [];
  }, [selectedProfile]);
  // 站内递归抓取设置（旧配置无这些字段时按后端默认值展示：开启 / 深度2 / 上限20页）
  const crawlEnabled = selectedProfile
    ? selectedProfile.config.webscrape_crawl_enabled !== false
    : true;
  const crawlDepth = Number(selectedProfile?.config.webscrape_crawl_depth ?? 2) || 0;
  const crawlMaxPages =
    Number(selectedProfile?.config.webscrape_crawl_max_pages ?? 20) || 20;
  const crawlPageLimit = Math.max(crawlMaxPages, profileUrls.length);

  const profileOptions = profiles.map((p) => {
    const urls = Array.isArray(p.config.webscrape_urls)
      ? (p.config.webscrape_urls as string[]).filter((s) => s.trim())
      : [];
    return {
      value: p.id,
      label: `${p.name}（${urls.length} 个抓取 URL${urls.length ? "" : "，未配置"}）`,
    };
  });

  // 加载 Dify 知识库列表（确认下载时选择目标知识库用）
  const loadDatasets = () => {
    setDatasetsLoading(true);
    setDatasetsError(null);
    listDifyDatasets()
      .then((ds) => setDatasets(ds))
      .catch((e) => {
        setDatasets([]);
        setDatasetsError(String(e));
      })
      .finally(() => setDatasetsLoading(false));
  };

  const refreshHistory = () => {
    listWebScrapeTasks(10)
      .then((r) => setHistoryTasks(r.tasks))
      .catch(() => setHistoryTasks([]));
  };

  // ★ 入库台账：每条确认入库的抓取内容在数据库（webscrape_records 表）里的一行
  const refreshRecords = () => {
    listWebScrapeRecords(100)
      .then((r) => setRecords(r.records))
      .catch(() => setRecords([]));
  };

  // ---- 抓取 ----
  const handleRun = async () => {
    if (!selectedProfileId) return;
    setRunning(true);
    setSelectedUrls([]);
    setFileOpen(false);
    try {
      const r = await runWebScrape(selectedProfileId);
      if (r.error) {
        Modal.error({ title: "抓取失败", content: r.error });
        return;
      }
      setTask(r.task);
      refreshHistory();
    } catch (e) {
      Modal.error({ title: "抓取失败", content: String(e) });
    } finally {
      setRunning(false);
    }
  };

  // ---- 预览（确认下载前的抓取内容预览：正文 Markdown / 附件元信息）----
  const handlePreview = async (index: number) => {
    if (!task) return;
    setPreviewIdx(index);
    setPreviewOpen(true);
    setPreviewLoading(true);
    setPreview(null);
    try {
      const p = await previewWebScrapeItem(task.id, index);
      setPreview(p);
    } catch (e) {
      setPreview({
        url: "error",
        kind: "content",
        title: String(e),
      });
    } finally {
      setPreviewLoading(false);
    }
  };

  // ---- 单行确认下载（★ 第 1 步：确认并落地，不触发流水线）----
  const handleRowConfirm = (url: string, index: number) => {
    setSelectedUrls([url]);
    setConfirmProfileId(selectedProfileId);
    setConfirmDatasetId(undefined); // 每次确认重新选目标知识库，避免误入上个库
    setFileOpen(false);
    setFileIdx(index);
    // 打开弹窗即重拉知识库列表：页面加载时后端若在重启会导致下拉为空，这里自愈
    loadDatasets();
    setConfirmOpen(true);
  };

  // ---- 确认下载提交（成功后自动打开该文件的「文件预览」抽屉）----
  const handleConfirmSubmit = async () => {
    if (!task || !confirmProfileId || !confirmDatasetId || selectedUrls.length === 0) return;
    setConfirming(true);
    try {
      const r = await confirmWebScrapeTask(
        task.id,
        selectedUrls,
        confirmProfileId,
        confirmDatasetId
      );
      setTask(r.task);
      setSelectedUrls([]); // 行级确认：完成后清空，避免残留到下次弹窗
      setConfirmOpen(false);
      refreshHistory();
      refreshRecords();
      const landedOk = r.landed.some((l) => l.ok);
      if (r.error) {
        Modal.warning({ title: "部分内容下载未完成", content: r.error });
      }
      if (landedOk && fileIdx >= 0) {
        // 下载成功后进入第 2 步：打开该文件的预览，点「确定」才入库
        setFileOpen(true);
      }
    } catch (e) {
      Modal.error({ title: "确认下载失败", content: String(e) });
      setConfirmOpen(false);
    } finally {
      setConfirming(false);
    }
  };

  /** 打开「文件预览」抽屉（confirm 下载后 / 查看已入库文件的落地文件） */
  const openFilePreview = (index: number) => {
    setFileIdx(index);
    setFileOpen(true);
  };

  // 文件预览抽屉当前条目（从最新 task 派生：入库完成后行状态会自动刷新）
  const fileItem: WebScrapeItem | null =
    fileIdx >= 0 && task ? (task.items[fileIdx] ?? null) : null;

  // ---- 表格 ----
  const columns: ColumnsType<WebScrapeItem> = [
    {
      title: "类型",
      dataIndex: "kind",
      width: 90,
      render: (kind: string, it) =>
        !it.ok ? (
          <Tag color="red">失败</Tag>
        ) : kind === "attachment" ? (
          <Tag color="blue">附件文件</Tag>
        ) : (
          <Tag color="cyan">网页内容</Tag>
        ),
    },
    {
      title: "标题 / 文件名",
      key: "title",
      render: (_, it) => (
        <Space direction="vertical" size={0}>
          <Text strong={it.ok && !it.truncated}>{it.ok ? it.title : it.url}</Text>
          {it.ok && (it.filename || it.rel_path) && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {it.kind === "attachment" ? it.filename : it.title}
              {!!it.depth && ` · 递归第 ${it.depth} 层`}
            </Text>
          )}
        </Space>
      ),
    },
    {
      title: "URL",
      dataIndex: "url",
      ellipsis: true,
      render: (url: string) => (
        <Text copyable style={{ fontSize: 12 }}>
          {url}
        </Text>
      ),
    },
    {
      // ★ 2026-09 页面更新时间 + 与上次入库比对（网站未更新检测）
      title: "更新时间 / 更新检测",
      key: "update",
      width: 150,
      render: (_, it) => {
        if (!it.ok) return <Text type="secondary">-</Text>;
        return (
          <Space direction="vertical" size={2}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {it.kind === "content" ? it.page_time ?? "页面未标注时间" : "附件文件"}
            </Text>
            {it.unchanged && (
              <Tooltip
                title={`内容与上次成功入库完全一致（上次 ${it.prev_ingested_at ?? "-"} 入库${it.prev_dataset_name ? `到「${it.prev_dataset_name}」` : ""}）`}
              >
                <Tag color="success" style={{ marginInlineEnd: 0 }}>
                  与上次一致 · 未更新
                </Tag>
              </Tooltip>
            )}
          </Space>
        );
      },
    },
    {
      title: "规模",
      key: "size",
      width: 90,
      align: "right",
      render: (_, it) => {
        if (!it.ok) return "-";
        if (it.kind === "attachment")
          return (it.size ?? 0) > 1024 ? `${((it.size ?? 0) / 1024).toFixed(1)}KB` : `${it.size ?? 0}B`;
        return (
          <>
            {it.char_count?.toLocaleString()} 字
            {it.truncated && <Text type="warning">（截断）</Text>}
          </>
        );
      },
    },
    {
      title: "状态",
      key: "status",
      width: 120,
      render: (_, it) => {
        if (!it.ok) return <Tag color="red">失败</Tag>;
        if (!it.confirmed) return <Tag>待确认下载</Tag>;
        const meta = INGEST_STATUS_META[it.ingest_status ?? ""] ?? { color: "blue", label: "已确认" };
        return (
          <Tooltip
            title={
              it.ingest_status === "downloaded"
                ? "文件已下载，请点「预览文件」确认内容后点确定入库"
                : it.ingest_status === "error"
                  ? it.ingest_error ?? "入库失败，可重新解析"
                  : undefined
            }
          >
            <Tag color={meta.color}>{meta.label}</Tag>
          </Tooltip>
        );
      },
    },
    {
      title: "说明",
      dataIndex: "error",
      ellipsis: true,
      render: (err: string | null | undefined, it) => {
        if (!it.ok && err) return <Text type="danger">{err}</Text>;
        if (it.ingest_error) return <Text type="warning">{it.ingest_error}</Text>;
        return <Text type="secondary">-</Text>;
      },
    },
    {
      // ★ 2026-08-31 溯源：确认下载后显示入到哪个知识库
      title: "入库知识库",
      key: "dataset",
      width: 120,
      render: (_, it) =>
        it.dataset_name || it.dataset_id ? (
          <Tooltip title={it.dataset_id ?? ""}>
            <Tag color="purple">{it.dataset_name ?? it.dataset_id}</Tag>
          </Tooltip>
        ) : (
          <Text type="secondary">-</Text>
        ),
    },
    {
      title: "操作",
      key: "action",
      width: 180,
      render: (_, it, idx) =>
        it.ok && (
          <Space size={0} wrap>
            {it.confirmed ? (
              // 已确认下载 → 预览下载后的真实文件（下载阶段按钮在文件预览里点确定入库）
              <Button type="link" size="small" onClick={() => openFilePreview(idx)}>
                预览文件
              </Button>
            ) : (
              <>
                <Button type="link" size="small" onClick={() => handlePreview(idx)}>
                  {it.kind === "attachment" ? "详情" : "预览"}
                </Button>
                {/* ★ 2026-09-02 第 1 步：确认下载（落地 pending/，不触发流水线） */}
                <Button
                  type="link"
                  size="small"
                  disabled={confirming}
                  onClick={() => handleRowConfirm(it.url, idx)}
                >
                  确认下载
                </Button>
              </>
            )}
          </Space>
        ),
    },
  ];

  // ★ 2026-09 更新检测汇总：待确认任务中与上次成功入库完全一致（未更新）的项数
  const unchangedCount = task?.items.filter((i) => i.ok && i.unchanged).length ?? 0;

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <div>
        <Title level={4} style={{ margin: 0 }}>
          网站抓取
        </Title>
        <Paragraph type="secondary" style={{ marginTop: 4, marginBottom: 0 }}>
          知识库内容外延：先选<strong>网站抓取配置</strong>（其「抓取网站 URL 列表」
          即抓取来源，无需手动输入），一键抓取正文 / 下载附件 →{" "}
          <strong>行上「确认下载」</strong>落地到待处理区 →{" "}
          <strong>自动打开下载文件的预览</strong>（PDF/Word/Excel/PPT 等直接在线看）→{" "}
          <strong>预览处点「确定并解析入库」</strong>，对该项走
          <code>① MinerU 解析</code> → <code>② 切分</code> →{" "}
          <code>③ 入库</code> 流水线。
        </Paragraph>
      </div>

      {/* ① 配置选择：先选网站抓取配置（其 URL 列表即抓取来源） */}
      <Card size="small" title="① 选择网站抓取配置（决定抓取来源）">
        {profiles.length === 0 ? (
          <Space direction="vertical" style={{ width: "100%" }}>
            <Alert
              type="warning"
              showIcon
              message="尚未创建「网站抓取配置」"
              description="请先到配置中心创建网站抓取配置（在文档处理配置基础上配置「抓取网站 URL 列表」），之后才能在此页抓取其内容。"
            />
            <Button type="primary" onClick={onOpenConfig}>
              去配置中心创建
            </Button>
          </Space>
        ) : (
          <Space direction="vertical" style={{ width: "100%" }}>
            <Space wrap>
              <Text strong>网站抓取配置：</Text>
              <Select
                style={{ minWidth: 360 }}
                placeholder="请选择网站抓取配置"
                value={selectedProfileId}
                onChange={(v) => setSelectedProfileId(v)}
                options={profileOptions}
                optionRender={(o) => (
                  <Space direction="vertical" size={0}>
                    <span>{o.label}</span>
                  </Space>
                )}
              />
              <Button type="link" onClick={onOpenConfig}>
                配置中心管理
              </Button>
            </Space>
            {selectedProfile && (
              <Text
                type={profileUrls.length ? "secondary" : "danger"}
                style={{ fontSize: 12 }}
              >
                {profileUrls.length
                  ? `该配置共 ${profileUrls.length} 个抓取 URL，抓取时全部处理（同域名校验）`
                  : "该配置未设置「抓取网站 URL 列表」：请到配置中心完善配置后重新选择"}
              </Text>
            )}
          </Space>
        )}
      </Card>

      <Row gutter={16}>
        {/* ② 抓取来源展示 + 一键抓取 */}
        <Col xs={24} lg={10}>
          <Card
            size="small"
            title="② 确认抓取来源并开始抓取"
            extra={selectedProfile && <Text type="secondary" style={{ fontSize: 12 }}>{selectedProfile.name}</Text>}
          >
            {!selectedProfile ? (
              <Empty description="请先在上方选择网站抓取配置" />
            ) : profileUrls.length === 0 ? (
              <Alert
                type="warning"
                showIcon
                message="该配置尚未配置抓取 URL 列表"
                description="请到配置中心的「网站抓取配置」中填写「抓取网站 URL 列表」（每行一个，可包含网页与附件链接）。"
              />
            ) : (
              <Space direction="vertical" size="middle" style={{ width: "100%" }}>
                <div>
                  <Typography.Text strong>
                    将抓取以下 {profileUrls.length} 个 URL
                  </Typography.Text>
                  <Space
                    direction="vertical"
                    size={2}
                    style={{ width: "100%", marginTop: 8 }}
                  >
                    {profileUrls.map((u, i) => (
                      <Space key={i} size={6} style={{ width: "100%" }}>
                        <Tag color={u.match(/\.(pdf|docx?|xlsx?|pptx?|txt|md|zip|rar)(\?|$)/i) ? "blue" : "cyan"}>
                          {u.match(/\.(pdf|docx?|xlsx?|pptx?|txt|md|zip|rar)(\?|$)/i)
                            ? "附件"
                            : "网页"}
                        </Tag>
                        <Text ellipsis style={{ fontSize: 12, maxWidth: 300 }} copyable>
                          {u}
                        </Text>
                      </Space>
                    ))}
                  </Space>
                </div>
                <Alert
                  type={crawlEnabled ? "info" : "warning"}
                  showIcon
                  message={
                    crawlEnabled
                      ? `站内递归抓取已开启：沿页面链接递归 ${crawlDepth} 层，单次最多约 ${crawlPageLimit} 页`
                      : "站内递归抓取未开启：只抓取上方 URL 列表本身"
                  }
                  description={`网页正文确认下载时渲染为 PDF、附件链接下载原文件，统一由 MinerU 解析后切分入库。${
                    crawlEnabled
                      ? "递归仅跟随同站链接（同域名；URL 带栏目路径时限同栏目），发现的子页面与附件一并进入待确认列表。"
                      : "如需抓取子页面，请到配置中心开启「站内递归抓取」。"
                  }`}
                />
                <Button
                  type="primary"
                  block
                  loading={running}
                  disabled={profileUrls.length === 0}
                  onClick={handleRun}
                >
                  {running
                    ? "抓取中..."
                    : `开始抓取（${profileUrls.length} 个 URL${crawlEnabled ? " + 递归子页" : ""}）`}
                </Button>
              </Space>
            )}
          </Card>
        </Col>

        {/* ③ 任务表格：预览 → 确认下载 → 文件预览确定入库 */}
        <Col xs={24} lg={14}>
          <Card
            size="small"
            title="③ 预览 → 确认下载 → 文件预览入库"
            extra={
              task ? (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  抓取成功 {task.ok_count}/{task.total}，已确认下载 {task.confirmed_count}
                </Text>
              ) : undefined
            }
          >
            {!task ? (
              <Empty description="尚无抓取任务：选择网站抓取配置后点击「开始抓取」" />
            ) : (
              <Space direction="vertical" size="middle" style={{ width: "100%" }}>
                {task.status === "pending" && unchangedCount > 0 && (
                  <Alert
                    type="warning"
                    showIcon
                    message={`检测到 ${unchangedCount} 项与上次成功入库的内容完全一致（网站未更新）`}
                    description="已逐条比对内容指纹：这些 URL 上次入库后网站没有变化，无需再次入库，可略过行上「确认下载」；若仍需重新整理（如换知识库），照常操作即可。"
                  />
                )}
                {task.status !== "pending" && (
                  <Alert
                    type="info"
                    showIcon
                    message={`任务已确认下载（${task.confirm_profile ?? ""} @ ${task.confirm_time ?? ""}）`}
                    description="每项内容下载后请在「预览文件」中确认文件无误，点右下角「确定并解析入库」才会真正走解析 → 切分 → Dify 入库。"
                  />
                )}
                <Table
                  size="small"
                  rowKey={(it, i) => String(i)}
                  columns={columns}
                  dataSource={task.items}
                  scroll={{ x: 1100 }}
                  pagination={task.total > 10 ? { pageSize: 10 } : false}
                />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  每行先点「确认下载」落地文件 → 自动打开该文件的预览，确认内容后点「确定并解析入库」；
                  同一任务的不同行可确认入到不同知识库。
                </Text>
              </Space>
            )}
          </Card>
        </Col>
      </Row>

      {/* ④ 预览抽屉（确认下载前的抓取内容预览） */}
      <Drawer
        title={preview ? (preview.kind === "attachment" ? `附件：${preview.filename ?? ""}` : `预览：${preview.title}`) : "预览"}
        width={720}
        open={previewOpen}
        onClose={() => setPreviewOpen(false)}
      >
        <Spin spinning={previewLoading}>
          {preview && preview.kind === "attachment" && (
            <Descriptions size="small" column={1} bordered>
              <Descriptions.Item label="文件名">{preview.filename}</Descriptions.Item>
              <Descriptions.Item label="大小">
                {(preview.size ?? 0) > 1024 ? `${((preview.size ?? 0) / 1024).toFixed(1)}KB` : `${preview.size ?? 0}B`}
              </Descriptions.Item>
              <Descriptions.Item label="来源 URL">
                <Text copyable>{preview.url}</Text>
              </Descriptions.Item>
              <Descriptions.Item label="说明">
                附件将在确认下载后进入待处理区，可先在线预览下载到的原文件，再点确定入库（由 MinerU 解析后切分入库）
              </Descriptions.Item>
            </Descriptions>
          )}
          {preview && preview.kind === "content" && (
            <>
              <Text type="secondary" style={{ fontSize: 12, display: "block", marginBottom: 8 }}>
                来源：{preview.url}
              </Text>
              {preview.content ? (
                <div style={{ maxHeight: "calc(100vh - 180px)", overflow: "auto" }}>
                  <MarkdownPreview content={preview.content} />
                </div>
              ) : (
                <Empty description="正文内容为空" />
              )}
            </>
          )}
        </Spin>
      </Drawer>

      {/* ⑤ 确认下载弹窗（第 1 步）：再次选择配置 + 目标知识库，只落地不解析 */}
      <Modal
        title="确认下载（第 1 步 / 共 2 步）"
        open={confirmOpen}
        onCancel={() => setConfirmOpen(false)}
        onOk={handleConfirmSubmit}
        okText="确认并下载"
        cancelText="取消"
        confirmLoading={confirming}
        okButtonProps={{ disabled: !confirmProfileId || !confirmDatasetId }}
      >
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <Alert
            type="info"
            showIcon
            message={`本次将确认下载 ${selectedUrls.length} 项内容`}
            description="确认后先把文件落地到待处理区（网页正文渲染为 PDF、附件保留原文件），并自动打开该文件的预览。文件预览核对无误后，点「确定并解析入库」才执行 MinerU 解析 → 切分 → Dify 入库。"
          />
          <div>
            <Text strong>选择入库使用的配置方案（仅网站抓取配置，决定切分参数）：</Text>
            <div style={{ marginTop: 8 }}>
              <Radio.Group
                value={confirmProfileId}
                onChange={(e) => setConfirmProfileId(e.target.value)}
                style={{ display: "flex", flexDirection: "column", gap: 8 }}
              >
                {profiles.map((p) => (
                  <Radio key={p.id} value={p.id}>
                    {p.name}
                    {p.id === selectedProfileId && <Text type="secondary">（抓取时所用）</Text>}
                  </Radio>
                ))}
              </Radio.Group>
            </div>
          </div>
          {/* ★ 2026-08-31 目标知识库：每次确认时选择，可把不同批次内容入到不同知识库 */}
          <div>
            <Text strong>
              目标知识库（本次确认的内容入到这个知识库）：
            </Text>
            <div style={{ marginTop: 8 }}>
              <Space.Compact style={{ width: "100%" }}>
                <Select
                  style={{ width: "100%" }}
                  placeholder={datasetsLoading ? "知识库加载中..." : "请选择目标知识库"}
                  value={confirmDatasetId}
                  onChange={setConfirmDatasetId}
                  loading={datasetsLoading}
                  disabled={!datasets.length && !datasetsLoading}
                  options={datasets.map((d) => ({
                    value: d.id,
                    label: `${d.name}（${d.document_count} 文档）`,
                  }))}
                  showSearch
                  optionFilterProp="label"
                />
                <Button onClick={loadDatasets} loading={datasetsLoading}>
                  刷新
                </Button>
              </Space.Compact>
              <Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 4 }}>
                同一抓取任务可分多批确认：每批勾选部分内容 → 选一个知识库 → 确认下载；
                下一批换一个知识库即可入到不同库。
              </Text>
              {datasetsError && (
                <Alert
                  type="warning"
                  showIcon
                  style={{ marginTop: 8 }}
                  message={`知识库列表加载失败：${datasetsError}`}
                />
              )}
            </div>
          </div>
        </Space>
      </Modal>

      {/* ⑥ 下载后文件预览抽屉（第 2 步：预览点确定 → 逐项解析入库） */}
      <WebScrapeFilePreview
        taskId={task?.id ?? ""}
        item={fileItem}
        index={fileIdx}
        open={fileOpen}
        onClose={() => setFileOpen(false)}
        onTaskUpdated={(tk) => {
          setTask(tk);
          refreshHistory();
          refreshRecords();
        }}
      />

      {/* ⑦ 历史任务 */}
      <Card size="small" title="历史抓取任务">
        <Table
          size="small"
          rowKey="id"
          pagination={false}
          dataSource={historyTasks}
          locale={{ emptyText: "暂无抓取任务" }}
          columns={[
            { title: "时间", dataIndex: "created_at", width: 160 },
            { title: "配置", dataIndex: "profile_name", width: 140 },
            {
              title: "抓取 URL 列表",
              dataIndex: "urls",
              ellipsis: true,
              render: (urls: string[] | undefined) =>
                urls && urls.length ? (
                  <Tooltip title={urls.join("\n")}>
                    <Text style={{ fontSize: 12 }} copyable={{ text: urls.join(", ") }}>
                      {urls.length === 1 ? urls[0] : `共 ${urls.length} 个 URL`}
                    </Text>
                  </Tooltip>
                ) : (
                  "-"
                ),
            },
            {
              title: "状态",
              dataIndex: "status",
              width: 110,
              render: (s: string) => <Tag color={STATUS_COLOR[s] ?? "default"}>{statusLabel(s)}</Tag>,
            },
            {
              title: "结果",
              key: "result",
              width: 160,
              render: (_, t) => (
                <Text style={{ fontSize: 12 }}>
                  成功 {t.ok_count}/{t.total}，确认下载 {t.confirmed_count}
                </Text>
              ),
            },
            {
              title: "操作",
              key: "action",
              width: 90,
              render: (_, t) => (
                <Button
                  type="link"
                  size="small"
                  loading={taskLoading}
                  onClick={() => {
                    setTaskLoading(true);
                    getWebScrapeTask(t.id)
                      .then((tk) => {
                        setTask(tk);
                        setSelectedUrls([]);
                        setFileOpen(false);
                      })
                      .finally(() => setTaskLoading(false));
                  }}
                >
                  查看
                </Button>
              ),
            },
          ]}
        />
      </Card>

      {/* ⑧ 入库台账：每条入库内容在 webscrape_records 表里的记录（独立于文档上传的 manifest） */}
      <Card
        size="small"
        title="入库记录（webscrape_records 表）"
        extra={
          <Button type="link" size="small" onClick={refreshRecords}>
            刷新
          </Button>
        }
      >
        <Table
          size="small"
          rowKey="id"
          pagination={records.length > 10 ? { pageSize: 10 } : false}
          dataSource={records}
          locale={{ emptyText: "暂无入库记录：行上点「确认下载」→ 文件预览中点「确定并解析入库」后，每条内容会在此留档" }}
          columns={[
            { title: "入库时间", dataIndex: "created_at", width: 150 },
            {
              // ★ 2026-09 抓取内容本身在网站上的更新时间（下次抓取比对“是否更新”用）
              title: "页面更新时间",
              key: "page_time",
              width: 110,
              render: (_, r) =>
                r.page_time ? (
                  <Tooltip title={`内容指纹：${(r.content_hash ?? "").slice(0, 12)}${r.content_hash ? "…" : "（旧记录无指纹，无法比对）"}`}>
                    <Text style={{ fontSize: 12 }}>{r.page_time}</Text>
                  </Tooltip>
                ) : (
                  <Text type="secondary">-</Text>
                ),
            },
            {
              title: "标题 / URL",
              key: "title",
              render: (_, r) => (
                <Space direction="vertical" size={0} style={{ width: "100%" }}>
                  <Text strong ellipsis style={{ maxWidth: 260 }}>
                    {r.title || r.filename || "-"}
                  </Text>
                  <Text
                    type="secondary"
                    ellipsis
                    copyable
                    style={{ fontSize: 12, maxWidth: 260 }}
                  >
                    {r.url}
                  </Text>
                </Space>
              ),
            },
            {
              title: "类型",
              key: "kind",
              width: 100,
              render: (_, r) => (
                <Space size={4}>
                  {r.kind === "attachment" ? (
                    <Tag color="blue">附件</Tag>
                  ) : (
                    <Tag color="cyan">网页</Tag>
                  )}
                  {!!r.depth && <Tag>第{r.depth}层</Tag>}
                </Space>
              ),
            },
            {
              title: "入库知识库",
              key: "dataset",
              width: 130,
              render: (_, r) =>
                r.dataset_name || r.dataset_id ? (
                  <Tooltip title={r.dataset_id ?? ""}>
                    <Tag color="purple">{r.dataset_name ?? r.dataset_id}</Tag>
                  </Tooltip>
                ) : (
                  <Text type="secondary">-</Text>
                ),
            },
            {
              title: "状态",
              dataIndex: "status",
              width: 90,
              render: (s: string) => {
                const map: Record<string, [string, string]> = {
                  landed: ["default", "已下载"],
                  parsed: ["gold", "已解析"],
                  ingested: ["green", "已入库"],
                  error: ["red", "失败"],
                };
                const [color, label] = map[s] ?? ["default", s ?? "-"];
                return <Tag color={color}>{label}</Tag>;
              },
            },
            {
              title: "Dify 文档 / 错误",
              key: "note",
              ellipsis: true,
              render: (_, r) =>
                r.error_msg ? (
                  <Tooltip title={r.error_msg}>
                    <Text type="danger" style={{ fontSize: 12 }}>
                      {r.error_msg}
                    </Text>
                  </Tooltip>
                ) : r.dify_doc_id ? (
                  <Text copyable style={{ fontSize: 12 }}>
                    {r.dify_doc_id}
                  </Text>
                ) : (
                  <Text type="secondary">-</Text>
                ),
            },
            {
              title: "配置",
              dataIndex: "profile_name",
              width: 110,
              ellipsis: true,
              render: (v: string | null | undefined) => v || "-",
            },
          ]}
        />
      </Card>
    </Space>
  );
}

function statusLabel(s: string): string {
  switch (s) {
    case "pending":
      return "待确认";
    case "confirmed":
      return "已确认下载";
    case "done":
      return "已完成";
    case "cancelled":
      return "已取消";
    default:
      return s;
  }
}
