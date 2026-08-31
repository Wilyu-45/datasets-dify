import {
  Button,
  Card,
  Col,
  Descriptions,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Row,
  Segmented,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import { MinusCircleOutlined, PlusOutlined } from "@ant-design/icons";
import { useEffect, useMemo, useState } from "react";
import {
  activateConfigProfile,
  createConfigProfile,
  deleteConfigProfile,
  getConfigProfileTypes,
  getConfigSchema,
  listChunkStrategies,
  listConfigProfiles,
  listDifyDatasets,
  listRunConfigLogs,
  updateConfigProfile,
  type ConfigFieldDef,
  type ConfigProfile,
  type ConfigProfileTypeDef,
  type DifyDatasetItem,
  type RunConfigLogItem,
} from "../api/client";

const { Title, Paragraph, Text } = Typography;

/** 配置方案类型 → 展示名（与后端 PROFILE_TYPES 对齐）。 */
const PROFILE_TYPE_LABELS: Record<string, string> = {
  upload: "文档处理配置",
  webscrape: "网站抓取配置",
};

/** 配置方案类型 → Tag 颜色。 */
const PROFILE_TYPE_COLORS: Record<string, string> = {
  upload: "blue",
  webscrape: "purple",
};

/** 处理来源标识 → 中文展示。 */
const SOURCE_LABELS: Record<string, string> = {
  upload_single: "单文件上传",
  upload_batch: "批量上传",
  upload_reingest: "重跑入库",
  pipeline_api: "流水线",
};

/** 流水线状态 → Tag 颜色。 */
const STATUS_COLORS: Record<string, string> = {
  ok: "green",
  partial: "orange",
  failed: "red",
  error: "red",
};

/** 按字段类型渲染表单控件（受控）。 */
function FieldControl({
  field,
  value,
  onChange,
}: {
  field: ConfigFieldDef;
  value: number | boolean | string | string[] | undefined;
  onChange: (v: number | boolean | string | string[]) => void;
}) {
  if (field.type === "bool") {
    return <Switch checked={Boolean(value)} onChange={(v) => onChange(v)} />;
  }
  if (field.type === "int" || field.type === "float") {
    return (
      <InputNumber
        style={{ width: "100%" }}
        min={field.min}
        max={field.max}
        step={field.step ?? (field.type === "float" ? 0.01 : 1)}
        value={value as number}
        onChange={(v) => onChange(v ?? 0)}
      />
    );
  }
  if (field.type === "urls") {
    // 抓取网站 URL 列表：可增删的多行输入
    const list = Array.isArray(value) ? (value as string[]) : [];
    return (
      <Space direction="vertical" size={4} style={{ width: "100%" }}>
        {list.map((u, i) => (
          <Space.Compact key={i} style={{ width: "100%" }}>
            <Input
              value={u}
              placeholder="https://example.com/xxx（每行一个 URL）"
              onChange={(e) => {
                const next = [...list];
                next[i] = e.target.value;
                onChange(next);
              }}
            />
            <Button
              icon={<MinusCircleOutlined />}
              onClick={() => onChange(list.filter((_, j) => j !== i))}
            />
          </Space.Compact>
        ))}
        <Button
          type="dashed"
          block
          size="small"
          icon={<PlusOutlined />}
          onClick={() => onChange([...list, ""])}
        >
          添加 URL
        </Button>
      </Space>
    );
  }
  return (
    <Input
      value={value as string}
      onChange={(e) => onChange(e.target.value)}
      placeholder={field.description}
    />
  );
}

export default function ConfigPage() {
  const [profiles, setProfiles] = useState<ConfigProfile[]>([]);
  // ★ 2026-08-31 两套配置独立激活：按类型的激活 ID（upload/webscrape 互不顶替）
  const [activeIds, setActiveIds] = useState<Record<string, string | undefined>>({});
  const [fields, setFields] = useState<ConfigFieldDef[]>([]);
  const [datasets, setDatasets] = useState<DifyDatasetItem[]>([]);
  const [strategies, setStrategies] = useState<{ key: string; name: string }[]>([]);
  const [profileTypes, setProfileTypes] = useState<ConfigProfileTypeDef[]>([]);
  /** 当前查看/创建的配置类型：upload=文档处理 / webscrape=网站抓取 */
  const [activeType, setActiveType] = useState<string>("upload");
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<ConfigProfile | null>(null);
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm();
  // 处理配置记录（每次实际处理时落库的配置快照）
  const [runLogs, setRunLogs] = useState<RunConfigLogItem[]>([]);
  const [runLogsLoading, setRunLogsLoading] = useState(false);
  // 配置项单独受控（不依赖 Form.Item 的 valuePropName 注入，Switch/Input 兼容性更好）
  const [configValues, setConfigValues] = useState<
    Record<string, number | boolean | string | string[]>
  >({});

  const [msgApi, contextHolder] = message.useMessage();

  const loadRunLogs = async () => {
    setRunLogsLoading(true);
    try {
      const r = await listRunConfigLogs(50);
      setRunLogs(r.rows);
    } finally {
      setRunLogsLoading(false);
    }
  };

  const load = async () => {
    const [pr, sch, ds, st, pt] = await Promise.all([
      listConfigProfiles(),
      getConfigSchema(),
      listDifyDatasets(),
      listChunkStrategies(),
      getConfigProfileTypes(),
    ]);
    setProfiles(pr.profiles);
    setActiveIds(pr.active_profile_ids ?? {});
    setFields(sch.fields);
    setDatasets(ds);
    setStrategies(st.strategies);
    setProfileTypes(pt.types);
  };

  useEffect(() => {
    load().catch((e) => msgApi.error(`加载配置失败：${e?.message ?? e}`));
    loadRunLogs().catch((e) => msgApi.error(`加载处理配置记录失败：${e?.message ?? e}`));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const fieldMap = useMemo(() => {
    const m = new Map<string, ConfigFieldDef>();
    for (const f of fields) m.set(f.key, f);
    return m;
  }, [fields]);

  /** 用默认值（可基于指定方案）构造初始 configValues。 */
  const buildInit = (base?: ConfigProfile) => {
    const init: Record<string, number | boolean | string | string[]> = {};
    for (const f of fields) {
      // 非当前类型专属的字段不初始化（如网站抓取配置不含知识库 ID）
      const ts = f.types ?? [];
      if (ts.length && !ts.includes(activeType)) continue;
      init[f.key] = base?.config[f.key] ?? f.default;
    }
    return init;
  };

  const openCreate = () => {
    setEditing(null);
    const base = profiles.find((p) => p.id === activeIds[activeType]);
    form.setFieldsValue({ name: "" });
    setConfigValues(buildInit(base));
    setModalOpen(true);
  };

  const openEdit = (p: ConfigProfile) => {
    setEditing(p);
    form.setFieldsValue({ name: p.name });
    setConfigValues(buildInit(p));
    setModalOpen(true);
  };

  const handleSave = async () => {
    const { name } = await form.validateFields();
    setSaving(true);
    try {
      if (editing) {
        await updateConfigProfile(editing.id, { name, config: configValues });
        msgApi.success("配置方案已更新");
      } else {
        await createConfigProfile(name, configValues, activeType);
        msgApi.success("配置方案已创建");
      }
      setModalOpen(false);
      await load();
    } catch (e) {
      msgApi.error(`保存失败：${String(e)}`);
    } finally {
      setSaving(false);
    }
  };

  const handleActivate = async (p: ConfigProfile) => {
    try {
      await activateConfigProfile(p.id);
      msgApi.success(`已激活「${p.name}」`);
      await load();
    } catch (e) {
      msgApi.error(`激活失败：${String(e)}`);
    }
  };

  const handleDelete = async (p: ConfigProfile) => {
    try {
      const r = await deleteConfigProfile(p.id);
      msgApi.success(`已删除「${p.name}」`);
      setActiveIds(r.active_profile_ids ?? {});
      await load();
    } catch (e) {
      msgApi.error(`删除失败：${String(e)}`);
    }
  };

  const setCfg = (key: string, v: number | boolean | string | string[]) =>
    setConfigValues((prev) => ({ ...prev, [key]: v }));

  /** 当前选中的切分策略。 */
  const currentStrategy = String(configValues.chunk_strategy || "structure");
  const currentStrategyMeta = strategies.find((s) => s.key === currentStrategy) ?? null;

  /** 当前类型下应显示的配置字段（类型通用 + 该类型专属；再按切分策略过滤）。 */
  const visibleFields = useMemo(() => {
    return fields.filter((f) => {
      if (f.key === "dify_dataset_id" || f.key === "chunk_strategy") return false;
      // 类型过滤：字段标记了 types 且不含当前类型 → 不显示（如 webscrape_urls）
      const ts = f.types ?? [];
      if (ts.length && !ts.includes(activeType)) return false;
      const ss = f.strategies ?? [];
      // 无 strategies 元数据（旧 schema）时视为通用字段，全部显示
      if (!ss.length) return true;
      return ss.includes(currentStrategy);
    });
  }, [fields, currentStrategy, activeType]);

  /** 当前类型下的配置方案列表。 */
  const visibleProfiles = useMemo(
    () => profiles.filter((p) => (p.type ?? "upload") === activeType),
    [profiles, activeType]
  );
  const activeProfile = profiles.find((p) => p.id === activeIds[activeType]) ?? null;
  const typeLabel = (t?: string) => PROFILE_TYPE_LABELS[t ?? "upload"] ?? t ?? "文档处理配置";

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      {contextHolder}
      <div>
        <Title level={4} style={{ margin: 0 }}>
          配置中心
        </Title>
        <Paragraph type="secondary" style={{ marginTop: 4, marginBottom: 0 }}>
          配置分两套：<strong>文档处理配置</strong>（上传文档入库时使用）与{" "}
          <strong>网站抓取配置</strong>（网站抓取页使用，在文档处理基础上多一个
          「抓取网站 URL 列表」）。上传入库时将使用所选（当前激活）配置方案。
        </Paragraph>
      </div>

      <Card
        size="small"
        title={activeProfile ? `当前激活配置（${typeLabel(activeProfile.type)}）` : "当前激活配置"}
        extra={
          <Button type="primary" onClick={openCreate} disabled={!fields.length}>
            新建{typeLabel(activeType)}
          </Button>
        }
      >
        {activeProfile ? (
          <Descriptions size="small" column={{ xs: 1, md: 3 }}>
            <Descriptions.Item label="方案名称">
              <Space>
                {activeProfile.name}
                <Tag color={PROFILE_TYPE_COLORS[activeProfile.type ?? "upload"] ?? "default"}>
                  {typeLabel(activeProfile.type)}
                </Tag>
              </Space>
            </Descriptions.Item>
            {/* 知识库 ID：网站抓取配置不含此项（确认入库时另行选择） */}
            {(activeProfile.type ?? "upload") === "upload" && (
              <Descriptions.Item label="知识库 ID">
                {String(activeProfile.config.dify_dataset_id || "（未设置）")}
              </Descriptions.Item>
            )}
            <Descriptions.Item label="切分策略">
              {String(activeProfile.config.chunk_strategy || "structure")}
            </Descriptions.Item>
            {(activeProfile.type ?? "upload") === "webscrape" && (
              <Descriptions.Item label="抓取 URL 列表">
                {Array.isArray(activeProfile.config.webscrape_urls)
                  ? (activeProfile.config.webscrape_urls as string[]).length
                  : 0}{" "}
                个 URL
              </Descriptions.Item>
            )}
          </Descriptions>
        ) : (
          <Paragraph type="secondary" style={{ margin: 0 }}>
            尚未配置任何方案，请先新建并激活一个配置方案。
          </Paragraph>
        )}
      </Card>

      {/* 配置类型切换：两套配置分开管理 */}
      <Segmented
        block
        value={activeType}
        onChange={(v) => setActiveType(String(v))}
        options={profileTypes.length
          ? profileTypes.map((t) => ({ label: t.label, value: t.key }))
          : [
              { label: "文档处理配置", value: "upload" },
              { label: "网站抓取配置", value: "webscrape" },
            ]}
      />
      <Paragraph type="secondary" style={{ marginTop: 0, marginBottom: 4 }}>
        {profileTypes.find((t) => t.key === activeType)?.description ??
          (activeType === "webscrape"
            ? "网站抓取配置：在文档处理配置基础上多一个「抓取网站 URL 列表」；网站抓取页先选此配置，再抓取其配置的 URL 列表"
            : "上传文档（解析/切分/入库）时使用的配置方案")}
        {activeType === "upload" && visibleProfiles.length === 0 && (
          <span style={{ color: "#fa541c" }}>（暂无文档处理配置，上传页将不可用）</span>
        )}
        {activeType === "webscrape" && visibleProfiles.length === 0 && (
          <span style={{ color: "#fa541c" }}>（暂无网站抓取配置，网站抓取页将不可用）</span>
        )}
      </Paragraph>

      <Row gutter={[16, 16]}>
        {visibleProfiles.map((p) => {
          const isActive = p.id === activeIds[activeType];
          const urls = Array.isArray(p.config.webscrape_urls)
            ? (p.config.webscrape_urls as string[])
            : [];
          return (
            <Col xs={24} md={12} lg={8} key={p.id}>
              <Card
                size="small"
                title={
                  <Space>
                    {p.name}
                    <Tag color={PROFILE_TYPE_COLORS[p.type ?? "upload"] ?? "default"}>
                      {typeLabel(p.type)}
                    </Tag>
                    {isActive && <Tag color="green">当前激活</Tag>}
                  </Space>
                }
                extra={
                  <Space>
                    <Button size="small" onClick={() => openEdit(p)}>
                      编辑
                    </Button>
                    {!isActive && (
                      <Button
                        size="small"
                        type="link"
                        onClick={() => handleActivate(p)}
                      >
                        激活
                      </Button>
                    )}
                    <Popconfirm
                      title="删除该配置方案？"
                      onConfirm={() => handleDelete(p)}
                    >
                      <Button size="small" danger type="text">
                        删除
                      </Button>
                    </Popconfirm>
                  </Space>
                }
              >
                <Descriptions size="small" column={1} colon={false}>
                  {/* 知识库 ID：仅文档处理配置显示（网站抓取配置入库时另行选择） */}
                  {(p.type ?? "upload") === "upload" && (
                    <Descriptions.Item label="知识库 ID">
                      {String(p.config.dify_dataset_id || "（未设置）")}
                    </Descriptions.Item>
                  )}
                  <Descriptions.Item label="切分策略">
                    {String(p.config.chunk_strategy || "structure")}
                  </Descriptions.Item>
                  <Descriptions.Item label="目标字符数">
                    {String(p.config.chunk_target_chars ?? "-")}
                  </Descriptions.Item>
                  <Descriptions.Item label="硬上限">
                    {String(p.config.chunk_hard_limit ?? "-")}
                  </Descriptions.Item>
                  {(p.type ?? "upload") === "webscrape" && (
                    <Descriptions.Item label="抓取 URL 列表">
                      {urls.length === 0 ? (
                        <Text type="danger" style={{ fontSize: 12 }}>
                          未配置（网站抓取页不可用）
                        </Text>
                      ) : (
                        <Space direction="vertical" size={2} style={{ width: "100%" }}>
                          {urls.slice(0, 3).map((u, i) => (
                            <Text key={i} ellipsis style={{ fontSize: 12, maxWidth: 260 }}>
                              {u}
                            </Text>
                          ))}
                          {urls.length > 3 && (
                            <Tooltip title={urls.join("\n")}>
                              <Text type="secondary" style={{ fontSize: 12 }}>
                                …共 {urls.length} 个 URL
                              </Text>
                            </Tooltip>
                          )}
                        </Space>
                      )}
                    </Descriptions.Item>
                  )}
                  <Descriptions.Item label="更新时间">
                    {p.updated_at}
                  </Descriptions.Item>
                </Descriptions>
              </Card>
            </Col>
          );
        })}
        {visibleProfiles.length === 0 && (
          <Col span={24}>
            <Paragraph type="secondary">暂无{typeLabel(activeType)}，点击右上角「新建」创建。</Paragraph>
          </Col>
        )}
      </Row>

      {/* 处理配置记录：每次实际触发处理时落库的配置快照（process_config_log 表） */}
      <Card
        size="small"
        title="处理配置记录"
        extra={
          <Button size="small" onClick={() => loadRunLogs().catch((e) => msgApi.error(`加载失败：${String(e)}`))}>
            刷新
          </Button>
        }
      >
        <Paragraph type="secondary" style={{ marginTop: 0 }}>
          每次实际上传入库 / 触发流水线时，系统会把当时生效的配置快照（配置方案、切分参数、目标文件、结果状态）
          记录到数据库，用于追溯「这批文档当时是用什么配置处理的」。API Key 已脱敏。
        </Paragraph>
        <Table<RunConfigLogItem>
          size="small"
          rowKey="id"
          loading={runLogsLoading}
          dataSource={runLogs}
          pagination={{ pageSize: 10, showSizeChanger: false }}
          expandable={{
            expandedRowRender: (record) => (
              <div style={{ margin: 0 }}>
                {record.error && (
                  <Paragraph type="danger" style={{ marginTop: 0 }}>
                    错误信息：{record.error}
                  </Paragraph>
                )}
                <pre style={{ margin: 0, fontSize: 12, maxHeight: 320, overflow: "auto" }}>
                  {JSON.stringify(record.config, null, 2)}
                </pre>
              </div>
            ),
          }}
        >
          <Table.Column<RunConfigLogItem>
            title="处理时间"
            dataIndex="run_time"
            width={160}
            render={(v: string | null | undefined) => v ?? "-"}
          />
          <Table.Column<RunConfigLogItem>
            title="来源"
            dataIndex="source"
            width={110}
            render={(v: string | null | undefined) => SOURCE_LABELS[v ?? ""] ?? v ?? "-"}
          />
          <Table.Column<RunConfigLogItem>
            title="配置方案"
            dataIndex="profile_name"
            width={150}
            render={(v: string | null | undefined, record) =>
              v ? (
                <Tooltip title={record.profile_id ?? ""}>{v}</Tooltip>
              ) : (
                <Text type="secondary">环境默认</Text>
              )
            }
          />
          <Table.Column<RunConfigLogItem>
            title="切分策略"
            dataIndex="chunk_strategy"
            width={110}
            render={(v: string | null | undefined, record) =>
              v ?? String(record.config?.chunk_strategy ?? "-")
            }
          />
          <Table.Column<RunConfigLogItem>
            title="知识库 ID"
            dataIndex="dataset_id"
            width={140}
            ellipsis
            render={(v: string | null | undefined, record) => {
              const id = v ?? String(record.config?.dify_dataset_id ?? "");
              return id ? (
                <Tooltip title={id}>{id.length > 10 ? `${id.slice(0, 10)}…` : id}</Tooltip>
              ) : (
                "-"
              );
            }}
          />
          <Table.Column<RunConfigLogItem>
            title="本批文件"
            width={80}
            render={(_, record) => (
              <Tooltip
                title={
                  record.target_stems?.length
                    ? record.target_stems.join("\n")
                    : "（未指定，处理全部待处理文档）"
                }
              >
                {record.target_stems?.length ?? 0}
              </Tooltip>
            )}
          />
          <Table.Column<RunConfigLogItem>
            title="状态"
            dataIndex="status"
            width={90}
            render={(v: string | null | undefined) =>
              v ? <Tag color={STATUS_COLORS[v] ?? "default"}>{v}</Tag> : "-"
            }
          />
          <Table.Column<RunConfigLogItem>
            title="耗时"
            dataIndex="duration_ms"
            width={90}
            render={(v: number | null | undefined) =>
              typeof v === "number" ? `${(v / 1000).toFixed(1)}s` : "-"
            }
          />
        </Table>
      </Card>

      <Modal
        title={editing ? `编辑${typeLabel(editing.type)}「${editing.name}」` : `新建${typeLabel(activeType)}`}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={handleSave}
        confirmLoading={saving}
        width={760}
        destroyOnClose
      >
        <Form form={form} layout="vertical">
          <Form.Item
            name="name"
            label="方案名称"
            rules={[{ required: true, message: "请输入方案名称" }]}
          >
            <Input placeholder="例如：默认配置 / 论文精读 / 合同归档" />
          </Form.Item>

          <Space direction="vertical" size="small" style={{ width: "100%" }}>
            {/* 网站抓取配置专属：抓取网站 URL 列表 */}
            {activeType === "webscrape" && fieldMap.get("webscrape_urls") && (
              <div
                style={{
                  padding: 12,
                  border: "1px solid #f2e6ff",
                  borderRadius: 8,
                  backgroundColor: "#f9f0ff",
                }}
              >
                <Typography.Text strong>
                  {fieldMap.get("webscrape_urls")?.label ?? "抓取网站 URL 列表"}{" "}
                  <Tag color="purple">网站抓取专用</Tag>
                </Typography.Text>
                <Typography.Paragraph
                  type="secondary"
                  style={{ fontSize: 12, marginTop: 4, marginBottom: 8 }}
                >
                  {fieldMap.get("webscrape_urls")?.description}
                </Typography.Paragraph>
                <FieldControl
                  field={fieldMap.get("webscrape_urls")!}
                  value={configValues.webscrape_urls}
                  onChange={(v) => setCfg("webscrape_urls", v)}
                />
              </div>
            )}

            {/* 知识库 ID：仅文档处理配置（网站抓取配置入库时在网站抓取页确认弹窗选择） */}
            {activeType === "upload" && (
              <div>
                <Typography.Text strong>
                  {fieldMap.get("dify_dataset_id")?.label ?? "知识库 ID"}
                </Typography.Text>
                <div style={{ marginTop: 4 }}>
                  <Select
                    showSearch
                    style={{ width: "100%" }}
                    placeholder="选择 Dify 知识库（可直接输入 ID）"
                    value={configValues.dify_dataset_id as string}
                    onChange={(v) => setCfg("dify_dataset_id", v)}
                    options={datasets.map((d) => ({
                      value: d.id,
                      label: `${d.name}（${d.id.slice(0, 8)}…）`,
                    }))}
                    optionFilterProp="label"
                  />
                </div>
              </div>
            )}

            {/* 切分策略 */}
            <div>
              <Typography.Text strong>
                {fieldMap.get("chunk_strategy")?.label ?? "切分策略"}
              </Typography.Text>
              <div style={{ marginTop: 4 }}>
                <Select
                  style={{ width: "100%" }}
                  value={configValues.chunk_strategy as string}
                  onChange={(v) => setCfg("chunk_strategy", v)}
                  options={strategies.map((s) => ({ value: s.key, label: s.name }))}
                />
              </div>
              <Typography.Paragraph
                type="secondary"
                style={{ fontSize: 12, marginTop: 6, marginBottom: 0 }}
              >
                已选择 <Tag color="blue">{currentStrategyMeta?.name ?? currentStrategy}</Tag>
                ：下方仅显示与该策略相关的配置项；切换策略后配置项会随之变化，
                已填写的参数仍会保留。
              </Typography.Paragraph>
            </div>

            {/* 当前策略相关的切分参数 */}
            <Row gutter={12}>
              {visibleFields.map((f) => (
                <Col xs={24} md={12} key={f.key}>
                  <div style={{ marginBottom: 12 }}>
                    <Typography.Text
                      strong
                      style={{ fontSize: 12 }}
                      title={f.description}
                    >
                      {f.label}
                    </Typography.Text>
                    <div style={{ marginTop: 4 }}>
                      <FieldControl
                        field={f}
                        value={configValues[f.key]}
                        onChange={(v) => setCfg(f.key, v)}
                      />
                    </div>
                  </div>
                </Col>
              ))}
            </Row>
          </Space>
        </Form>
      </Modal>
    </Space>
  );
}
