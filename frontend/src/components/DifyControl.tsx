import {
  Alert,
  AutoComplete,
  Button,
  Card,
  Col,
  Descriptions,
  Row,
  Space,
  Statistic,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import {
  CloudUploadOutlined,
  ExperimentOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import { useEffect, useState } from "react";
import {
  getDifyConfig,
  listDifyDatasets,
  triggerDifyUpload,
  updateDifyDatasetId,
  type DifyActionRecord,
  type DifyConfigInfo,
  type DifyDatasetItem,
  type DifyUploadReport,
} from "../api/client";

const { Text, Paragraph } = Typography;

interface Props {
  onAfterUpload: (r: DifyUploadReport) => void;
  lastReport: DifyUploadReport | null;
  loading: boolean;
  onLoadingChange: (v: boolean) => void;
}

const ACTION_COLORS: Record<string, string> = {
  uploaded: "green",
  skipped_done: "blue",
  failed: "red",
  dry_run: "gold",
};

const ACTION_LABELS: Record<string, string> = {
  uploaded: "已入库",
  skipped_done: "已入库跳过",
  failed: "失败",
  dry_run: "试运行",
};

export default function DifyControl({
  onAfterUpload,
  lastReport,
  loading,
  onLoadingChange,
}: Props) {
  const [config, setConfig] = useState<DifyConfigInfo | null>(null);
  const [configLoading, setConfigLoading] = useState(false);
  const [dryRun, setDryRun] = useState(false);
  const [force, setForce] = useState(false);
  const [datasets, setDatasets] = useState<DifyDatasetItem[]>([]);
  const [datasetsLoading, setDatasetsLoading] = useState(false);
  const [datasetIdInput, setDatasetIdInput] = useState("");
  const [savingDataset, setSavingDataset] = useState(false);
  const [msgApi, contextHolder] = message.useMessage();

  const loadConfig = async () => {
    setConfigLoading(true);
    try {
      const c = await getDifyConfig();
      setConfig(c);
      setDatasetIdInput(c.dataset_id);
    } catch (e) {
      msgApi.error(`加载 Dify 配置失败：${(e as Error).message}`);
    } finally {
      setConfigLoading(false);
    }
  };

  const loadDatasets = async () => {
    if (!config?.has_api_key) return;
    setDatasetsLoading(true);
    try {
      const list = await listDifyDatasets();
      setDatasets(list);
    } catch (e) {
      msgApi.warning(
        `知识库列表加载失败（仍可手动粘贴 ID）：${(e as Error).message}`
      );
    } finally {
      setDatasetsLoading(false);
    }
  };

  useEffect(() => {
    loadConfig();
  }, []);

  useEffect(() => {
    if (config?.has_api_key) {
      loadDatasets();
    }
  }, [config?.has_api_key]);

  const applyDatasetId = async (id?: string) => {
    const target = (id ?? datasetIdInput).trim();
    if (!target) {
      msgApi.warning("请输入知识库 ID");
      return;
    }
    setSavingDataset(true);
    try {
      const c = await updateDifyDatasetId(target);
      setConfig(c);
      setDatasetIdInput(c.dataset_id);
      msgApi.success("目标知识库已切换（已写回 backend/.env）");
    } catch (e) {
      msgApi.error(`切换知识库失败：${(e as Error).message}`);
    } finally {
      setSavingDataset(false);
    }
  };

  const datasetOptions = datasets.map((d) => ({
    value: d.id,
    label: `${d.name}${d.document_count > 0 ? `（${d.document_count} 文档）` : ""}`,
  }));

  const onRun = async () => {
    onLoadingChange(true);
    try {
      const report = await triggerDifyUpload(dryRun, force);
      onAfterUpload(report);
      msgApi.success(
        `Dify 入库完成：扫描 ${report.scanned}，成功 ${report.uploaded}，跳过 ${report.skipped_done}，失败 ${report.failed}`
      );
    } catch (e) {
      msgApi.error(`Dify 入库失败：${(e as Error).message}`);
    } finally {
      onLoadingChange(false);
    }
  };

  // 失败行的明细（仅展示前 10 条）
  const failedActions: DifyActionRecord[] =
    lastReport?.actions.filter((a) => a.action === "failed").slice(0, 10) ?? [];

  return (
    <Card
      title={
        <Space>
          <CloudUploadOutlined />
          <span>执行 §3.4 Dify 入库</span>
        </Space>
      }
      extra={
        <Space>
          <Tooltip title="开启后只识别不实际调用 Dify（不创建文档、不上传图片、不动 manifest）">
            <Space>
              <ExperimentOutlined />
              <Text>试运行</Text>
              <Switch checked={dryRun} onChange={setDryRun} />
            </Space>
          </Tooltip>
          <Tooltip title="已入库过（dify_status=done）的文档会重传；通常用于 Dify 端数据丢失时全量回灌">
            <Space>
              <ReloadOutlined />
              <Text>强制重传</Text>
              <Switch
                checked={force}
                onChange={setForce}
                disabled={dryRun}
              />
            </Space>
          </Tooltip>
          <Button
            type="primary"
            icon={<PlayCircleOutlined />}
            loading={loading}
            onClick={onRun}
            disabled={!config?.has_api_key && !dryRun}
          >
            {dryRun
              ? "试运行入库"
              : force
                ? "强制重传"
                : "执行入库"}
          </Button>
        </Space>
      }
    >
      {contextHolder}
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {/* 当前 Dify 配置 */}
        <Card
          size="small"
          type="inner"
          title={
            <Space>
              <SafetyCertificateOutlined />
              <span>当前 Dify 配置</span>
            </Space>
          }
          extra={
            <Button
              size="small"
              icon={<ReloadOutlined />}
              onClick={() => {
                loadConfig();
                loadDatasets();
              }}
              loading={configLoading || datasetsLoading}
            >
              刷新
            </Button>
          }
        >
          {config ? (
            <Descriptions
              size="small"
              column={2 as const}
              items={[
                {
                  key: "api_url",
                  label: "API 地址",
                  children: <code>{config.api_url}</code>,
                },
                {
                  key: "dataset_id",
                  label: "目标知识库",
                  span: 2,
                  children: config.has_api_key ? (
                    <Space direction="vertical" style={{ width: "100%" }} size={4}>
                      <Space.Compact style={{ width: "100%" }}>
                        <AutoComplete
                          style={{ width: "100%" }}
                          value={datasetIdInput}
                          options={datasetOptions}
                          onChange={setDatasetIdInput}
                          onSelect={(v) => applyDatasetId(v)}
                          placeholder="选择知识库或手动粘贴 dataset_id"
                          allowClear
                          notFoundContent={
                            datasetsLoading
                              ? "加载中…"
                              : "无匹配知识库（可手动粘贴 ID）"
                          }
                        />
                        <Button
                          type="primary"
                          loading={savingDataset}
                          onClick={() => applyDatasetId()}
                        >
                          应用
                        </Button>
                      </Space.Compact>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        从下拉选择或手动输入知识库 ID，点「应用」立即生效并写回
                        backend/.env（RAG_DIFY_DATASET_ID），重启后依然有效。
                      </Text>
                    </Space>
                  ) : (
                    <Tag color="red">
                      未配置 API Key，无法选择知识库（请在 backend/.env
                      配置 RAG_DIFY_API_KEY）
                    </Tag>
                  ),
                },
                {
                  key: "api_key",
                  label: "API Key",
                  children: config.has_api_key ? (
                    <Tag color="green">已配置</Tag>
                  ) : (
                    <Tag color="red">未配置（请在 backend/.env 配置 RAG_DIFY_API_KEY）</Tag>
                  ),
                },
                {
                  key: "indexing",
                  label: "索引技术",
                  children: <Tag color="blue">{config.indexing_technique}</Tag>,
                },
                {
                  key: "doc_form",
                  label: "文档形态",
                  children: <Tag color="purple">{config.doc_form}</Tag>,
                },
                {
                  key: "chunk_dirs",
                  label: "可入库目录数",
                  children: (
                    <Tag color={config.chunk_dir_count > 0 ? "green" : "default"}>
                      {config.chunk_dir_count} 个文档
                    </Tag>
                  ),
                },
                {
                  key: "output_dirs",
                  label: "已归档目录数",
                  children: (
                    <Tag color={config.output_dir_count > 0 ? "gold" : "default"}>
                      {config.output_dir_count} 个文档
                    </Tag>
                  ),
                },
                {
                  key: "output_dir",
                  label: "归档目录",
                  children: <code style={{ fontSize: 12 }}>{config.output_dir}</code>,
                },
              ]}
            />
          ) : (
            <Paragraph type="secondary" style={{ margin: 0 }}>
              加载中…
            </Paragraph>
          )}
        </Card>

        {/* 执行说明 */}
        <Text type="secondary" style={{ fontSize: 12 }}>
          遍历 <code>data/chunks/</code> 下所有以文档命名的子目录 →
          对每个目录在 Dify 创建一个文档（name = 目录名 / stem，方便溯源） →
          等待 <code>indexing_status=completed</code> →
          逐 chunk 上传引用图片到 Dify 并 <code>add_segments</code> →
          写回 <code>manifest.dify_doc_id</code> / <code>dify_status</code>。
        </Text>

        {!config?.has_api_key && !dryRun && (
          <Alert
            type="warning"
            showIcon
            message="未配置 Dify API Key"
            description="请在 backend/.env 设置 RAG_DIFY_API_KEY，或先打开「试运行」按钮预览待入库的文档列表。"
          />
        )}

        {/* 本次结果统计 */}
        <Row gutter={16}>
          <Col xs={12} md={6}>
            <Statistic title="本次扫描" value={lastReport?.scanned ?? 0} />
          </Col>
          <Col xs={12} md={6}>
            <Statistic
              title="已入库"
              value={lastReport?.uploaded ?? 0}
              valueStyle={{
                color: lastReport && lastReport.uploaded > 0 ? "#52c41a" : undefined,
              }}
            />
          </Col>
          <Col xs={12} md={6}>
            <Statistic
              title="已入库跳过"
              value={lastReport?.skipped_done ?? 0}
              valueStyle={{ color: "#1677ff" }}
            />
          </Col>
          <Col xs={12} md={6}>
            <Statistic
              title="失败"
              value={lastReport?.failed ?? 0}
              valueStyle={{
                color: lastReport && lastReport.failed > 0 ? "#cf1322" : undefined,
              }}
            />
          </Col>
        </Row>

        {/* 失败明细 */}
        {failedActions.length > 0 && (
          <div>
            <Space style={{ marginBottom: 8 }}>
              <WarningOutlined style={{ color: "#faad14" }} />
              <Text strong style={{ fontSize: 13 }}>
                本次失败明细（前 {failedActions.length} 条）
              </Text>
            </Space>
            <Table
              size="small"
              rowKey="stem"
              dataSource={failedActions}
              pagination={{ pageSize: 5, showSizeChanger: false }}
              columns={[
                {
                  title: "文档 stem",
                  dataIndex: "stem",
                  ellipsis: true,
                  width: 240,
                },
                {
                  title: "动作",
                  dataIndex: "action",
                  width: 100,
                  render: (v: string) => (
                    <Tag color={ACTION_COLORS[v] ?? "default"}>
                      {ACTION_LABELS[v] ?? v}
                    </Tag>
                  ),
                },
                {
                  title: "失败原因",
                  dataIndex: "error",
                  ellipsis: true,
                  render: (v: string | null | undefined) =>
                    v ? (
                      <span style={{ color: "#cf1322", fontSize: 12 }}>{v}</span>
                    ) : (
                      <span style={{ color: "#bbb" }}>—</span>
                    ),
                },
              ]}
            />
          </div>
        )}
      </Space>
    </Card>
  );
}
