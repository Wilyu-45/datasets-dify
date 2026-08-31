/**
 * 网站抓取页面（2026-08 新增：知识库内容外延，两步式确认）。
 *
 * 流程：
 *   ① 选择配置方案（其「抓取网站 URL」决定可抓取哪个网站）→ 解锁抓取
 *   ② 输入 URL 列表 → 抓取：网页正文转 Markdown、附件文件下载，
 *      生成「待确认任务」（此阶段不入库）
 *   ③ 预览内容（正文渲染 / 附件信息）→ 勾选确实需要的内容 →
 *      再次选择配置 → 点「确认并入库」→ 走 parse(MinerU) → 切分 → Dify 入库
 *
 * 布局：顶部配置选择；左侧配置信息 + URL 输入；右侧任务表格 + 预览抽屉；
 *       底部历史任务列表。
 */
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  Descriptions,
  Drawer,
  Empty,
  Input,
  Modal,
  Radio,
  Row,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useMemo, useState } from "react";
import {
  confirmWebScrapeTask,
  getWebScrapeTask,
  listConfigProfiles,
  listWebScrapeTasks,
  previewWebScrapeItem,
  runWebScrape,
  type ConfigProfile,
  type DifyActionRecord,
  type PipelineReport,
  type WebScrapeItem,
  type WebScrapePreviewResponse,
  type WebScrapeTask,
  type WebScrapeTaskListItem,
} from "../api/client";
import MarkdownPreview from "../components/MarkdownPreview";

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

export default function WebScrapePage({ onOpenConfig }: Props) {
  // ---- 配置选择（先选配置，才能去对应的网站抓取内容）----
  const [profiles, setProfiles] = useState<ConfigProfile[]>([]);
  const [selectedProfileId, setSelectedProfileId] = useState<string>();
  const [siteUrl, setSiteUrl] = useState("");

  // ---- 抓取 ----
  const [urlsText, setUrlsText] = useState("");
  const [running, setRunning] = useState(false);

  // ---- 任务与预览 ----
  const [task, setTask] = useState<WebScrapeTask | null>(null);
  const [taskLoading, setTaskLoading] = useState(false);
  const [selectedUrls, setSelectedUrls] = useState<string[]>([]);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewIdx, setPreviewIdx] = useState<number>(-1);
  const [preview, setPreview] = useState<WebScrapePreviewResponse | null>(null);

  // ---- 确认入库 ----
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [confirmProfileId, setConfirmProfileId] = useState<string>();
  const [confirming, setConfirming] = useState(false);
  const [confirmResult, setConfirmResult] = useState<{
    pipeline: PipelineReport | null;
    error: string | null;
  } | null>(null);

  // ---- 历史任务 ----
  const [historyTasks, setHistoryTasks] = useState<WebScrapeTaskListItem[]>([]);

  // 加载配置方案
  useEffect(() => {
    listConfigProfiles()
      .then((pr) => {
        setProfiles(pr.profiles);
        if (pr.active_profile_id) setSelectedProfileId(pr.active_profile_id);
      })
      .catch(() => setProfiles([]));
    refreshHistory();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 选中配置后展示其抓取网站 URL
  const selectedProfile = useMemo(
    () => profiles.find((p) => p.id === selectedProfileId) ?? null,
    [profiles, selectedProfileId]
  );
  const selectedSiteUrl = selectedProfile
    ? String(selectedProfile.config.webscrape_site_url || "")
    : "";

  const profileOptions = profiles.map((p) => ({
    value: p.id,
    label: `${p.name}${
      p.config.webscrape_site_url ? `（${String(p.config.webscrape_site_url)}）` : "（未配置抓取网站）"
    }`,
  }));

  const refreshHistory = () => {
    listWebScrapeTasks(10)
      .then((r) => setHistoryTasks(r.tasks))
      .catch(() => setHistoryTasks([]));
  };

  // ---- 抓取 ----
  const parseUrls = (): string[] =>
    urlsText
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean);

  const handleRun = async () => {
    if (!selectedProfileId) return;
    const urls = parseUrls();
    if (urls.length === 0) return;
    setRunning(true);
    setConfirmResult(null);
    setSelectedUrls([]);
    try {
      const r = await runWebScrape(selectedProfileId, urls);
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

  // ---- 预览 ----
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

  // ---- 确认入库 ----
  const confirmableItems = (task?.items ?? []).filter(
    (it) => it.ok && !it.confirmed
  );

  const handleConfirmSubmit = async () => {
    if (!task || !confirmProfileId || selectedUrls.length === 0) return;
    setConfirming(true);
    try {
      const r = await confirmWebScrapeTask(task.id, selectedUrls, confirmProfileId);
      setConfirmResult({ pipeline: r.pipeline, error: r.error });
      setTask(r.task);
      setConfirmOpen(false);
      refreshHistory();
    } catch (e) {
      Modal.error({ title: "确认入库失败", content: String(e) });
      setConfirmOpen(false);
    } finally {
      setConfirming(false);
    }
  };

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
      width: 100,
      render: (_, it) => {
        if (it.confirmed)
          return it.ingest_status === "ok" ? (
            <Tag color="green">已入库</Tag>
          ) : (
            <Tag color="red">入库失败</Tag>
          );
        return it.ok ? <Tag>待确认</Tag> : <Tag color="red">失败</Tag>;
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
      title: "操作",
      key: "action",
      width: 90,
      render: (_, it, idx) =>
        it.ok && (
          <Button
            type="link"
            size="small"
            onClick={() => handlePreview(idx)}
          >
            {it.kind === "attachment" ? "详情" : "预览"}
          </Button>
        ),
    },
  ];

  const difyActions: DifyActionRecord[] | null =
    confirmResult?.pipeline?.dify?.actions ?? null;

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <div>
        <Title level={4} style={{ margin: 0 }}>
          网站抓取
        </Title>
        <Paragraph type="secondary" style={{ marginTop: 4, marginBottom: 0 }}>
          知识库内容外延：先选择配置方案（决定可抓取的网站），输入网页 URL 后
          抓取正文 / 下载附件 → 在预览页确认内容确实是需要的 → 选择配置后
          确认入库，走
          <code>① MinerU 解析（附件）</code> → <code>② 切分</code> →{" "}
          <code>③ 入库</code> 流水线。
        </Paragraph>
      </div>

      {/* ① 配置选择：先选配置，才能去对应的网站抓取内容 */}
      <Card size="small" title="① 选择配置（决定抓取网站）">
        <Space direction="vertical" style={{ width: "100%" }}>
          <Space wrap>
            <Text strong>配置方案：</Text>
            <Select
              style={{ minWidth: 360 }}
              placeholder="请选择配置方案"
              value={selectedProfileId}
              onChange={(v) => {
                setSelectedProfileId(v);
                setConfirmResult(null);
              }}
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
              type={selectedSiteUrl ? "secondary" : "danger"}
              style={{ fontSize: 12 }}
            >
              {selectedSiteUrl
                ? `可抓取网站：${selectedSiteUrl}（仅限同域名页面 / 附件）`
                : "该配置未设置「抓取网站 URL」：请到配置中心完善配置后重新选择"}
            </Text>
          )}
        </Space>
      </Card>

      <Row gutter={16}>
        {/* ② 抓取输入 */}
        <Col xs={24} lg={10}>
          <Card size="small" title="② 输入 URL 并抓取" extra={selectedProfile && <Text type="secondary" style={{ fontSize: 12 }}>{selectedProfile.name}</Text>}>
            <Space direction="vertical" size="middle" style={{ width: "100%" }}>
              <div>
                <Typography.Text strong>网页 URL（每行一个）</Typography.Text>
                <textarea
                  value={urlsText}
                  onChange={(e) => setUrlsText(e.target.value)}
                  placeholder={
                    selectedSiteUrl
                      ? `${selectedSiteUrl}/some-page\n${selectedSiteUrl}/uploads/report.pdf`
                      : "请先在上方选择配置方案"
                  }
                  disabled={!selectedSiteUrl}
                  style={{
                    width: "100%",
                    minHeight: 140,
                    marginTop: 8,
                    padding: 8,
                    border: "1px solid #d9d9d9",
                    borderRadius: 6,
                    fontFamily: "monospace",
                    fontSize: 12,
                    resize: "vertical",
                  }}
                />
              </div>
              <Alert
                type="info"
                showIcon
                message="自动识别内容类型"
                description="网页 → 正文转 Markdown；PDF/DOCX 等附件链接 → 下载原文件，确认后由 MinerU 解析。"
              />
              <Button
                type="primary"
                block
                loading={running}
                disabled={!selectedSiteUrl || parseUrls().length === 0}
                onClick={handleRun}
              >
                {running
                  ? "抓取中..."
                  : `开始抓取${parseUrls().length ? `（${parseUrls().length} 个 URL）` : ""}`}
              </Button>
            </Space>
          </Card>
        </Col>

        {/* ③ 任务表格：预览 + 勾选确认 */}
        <Col xs={24} lg={14}>
          <Card
            size="small"
            title="③ 预览并确认内容"
            extra={
              task ? (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  抓取成功 {task.ok_count}/{task.total}，已确认 {task.confirmed_count}
                </Text>
              ) : undefined
            }
          >
            {!task ? (
              <Empty description="尚无抓取任务：选择配置后输入 URL 开始抓取" />
            ) : (
              <Space direction="vertical" size="middle" style={{ width: "100%" }}>
                {task.status !== "pending" && (
                  <Alert
                    type="warning"
                    showIcon
                    message={`任务已确认（${task.confirm_profile ?? ""} @ ${task.confirm_time ?? ""}），如需补充内容请新建抓取任务`}
                  />
                )}
                <Table
                  size="small"
                  rowKey={(it, i) => String(i)}
                  columns={columns}
                  dataSource={task.items}
                  pagination={task.total > 10 ? { pageSize: 10 } : false}
                  rowSelection={{
                    selectedRowKeys: selectedUrls.map((u) => String(task.items.findIndex((x) => x.url === u))),
                    onChange: (keys) => {
                      const urls = keys
                        .map((k) => task.items[Number(k)])
                        .filter((it): it is WebScrapeItem => !!it && it.ok && !it.confirmed)
                        .map((it) => it.url);
                      setSelectedUrls(urls);
                    },
                    getCheckboxProps: (it) => ({
                      disabled: !it.ok || it.confirmed,
                    }),
                  }}
                />
                <Space wrap>
                  <Button
                    type="primary"
                    disabled={selectedUrls.length === 0 || task.status !== "pending"}
                    onClick={() => {
                      setConfirmProfileId(selectedProfileId);
                      setConfirmResult(null);
                      setConfirmOpen(true);
                    }}
                  >
                    确认并入库（{selectedUrls.length} 项）
                  </Button>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    预览后勾选确实需要的内容，未勾选的不入库
                  </Text>
                </Space>

                {/* 确认后的流水线报告 */}
                {confirmResult && (
                  <Alert
                    type={confirmResult.error ? "error" : "success"}
                    showIcon
                    message="入库流程已执行"
                    description={
                      <Space direction="vertical" size={4}>
                        {confirmResult.error && <span>流水线失败：{confirmResult.error}</span>}
                        {confirmResult.pipeline && (
                          <span>
                            解析 {confirmResult.pipeline.parse?.parsed ?? confirmResult.pipeline.parse?.skipped_done ?? 0} 篇，
                            切分 {confirmResult.pipeline.chunk?.chunked ?? 0} 篇，
                            入库 {confirmResult.pipeline.dify?.uploaded ?? 0} 篇，
                            状态{" "}
                            <Tag color={confirmResult.pipeline.status === "ok" ? "green" : "orange"}>
                              {confirmResult.pipeline.status}
                            </Tag>
                            （耗时 {(confirmResult.pipeline.duration_ms / 1000).toFixed(1)}s）
                          </span>
                        )}
                        {difyActions && difyActions.length > 0 && (
                          <span>
                            {difyActions.map((a, i) => (
                              <Text key={i} style={{ fontSize: 12, display: "block" }}>
                                {a.stem}：{a.action === "uploaded" ? "已入库" : a.action}
                                {a.dify_doc_id ? `（${a.dify_doc_id}）` : ""}
                                {a.error ? ` - ${a.error}` : ""}
                              </Text>
                            ))}
                          </span>
                        )}
                      </Space>
                    }
                  />
                )}
              </Space>
            )}
          </Card>
        </Col>
      </Row>

      {/* ④ 预览抽屉 */}
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
                附件将在确认后下载到待处理区，由 MinerU 解析后切分入库
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

      {/* ⑤ 确认入库弹窗：再次选择配置 */}
      <Modal
        title="确认入库"
        open={confirmOpen}
        onCancel={() => setConfirmOpen(false)}
        onOk={handleConfirmSubmit}
        okText="确认并入库"
        cancelText="取消"
        confirmLoading={confirming}
        okButtonProps={{ disabled: !confirmProfileId }}
      >
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <Alert
            type="info"
            showIcon
            message={`已勾选 ${selectedUrls.length} 项内容`}
            description="确认后将把勾选内容落地并触发流水线：附件走 MinerU 解析 → 切分 → Dify 入库（此操作不可撤销）。"
          />
          <div>
            <Text strong>选择入库使用的配置方案：</Text>
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
        </Space>
      </Modal>

      {/* ⑥ 历史任务 */}
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
            { title: "抓取网站", dataIndex: "site_url", ellipsis: true },
            {
              title: "状态",
              dataIndex: "status",
              width: 100,
              render: (s: string) => <Tag color={STATUS_COLOR[s] ?? "default"}>{statusLabel(s)}</Tag>,
            },
            {
              title: "结果",
              key: "result",
              width: 160,
              render: (_, t) => (
                <Text style={{ fontSize: 12 }}>
                  成功 {t.ok_count}/{t.total}，确认 {t.confirmed_count}
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
                        setConfirmResult(null);
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
    </Space>
  );
}

function statusLabel(s: string): string {
  switch (s) {
    case "pending":
      return "待确认";
    case "confirmed":
      return "已确认";
    case "done":
      return "已完成";
    case "cancelled":
      return "已取消";
    default:
      return s;
  }
}