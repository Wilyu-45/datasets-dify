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
  Select,
  Space,
  Switch,
  Tag,
  Typography,
  message,
} from "antd";
import { useEffect, useMemo, useState } from "react";
import {
  activateConfigProfile,
  createConfigProfile,
  deleteConfigProfile,
  getConfigSchema,
  listChunkStrategies,
  listConfigProfiles,
  listDifyDatasets,
  updateConfigProfile,
  type ConfigFieldDef,
  type ConfigProfile,
  type DifyDatasetItem,
} from "../api/client";

const { Title, Paragraph } = Typography;

/** 按字段类型渲染表单控件（受控）。 */
function FieldControl({
  field,
  value,
  onChange,
}: {
  field: ConfigFieldDef;
  value: number | boolean | string | undefined;
  onChange: (v: number | boolean | string) => void;
}) {
  if (field.type === "bool") {
    return <Switch checked={Boolean(value)} onChange={onChange} />;
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
  const [activeId, setActiveId] = useState<string | null>(null);
  const [fields, setFields] = useState<ConfigFieldDef[]>([]);
  const [datasets, setDatasets] = useState<DifyDatasetItem[]>([]);
  const [strategies, setStrategies] = useState<{ key: string; name: string }[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<ConfigProfile | null>(null);
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm();
  // 配置项单独受控（不依赖 Form.Item 的 valuePropName 注入，Switch/Input 兼容性更好）
  const [configValues, setConfigValues] = useState<
    Record<string, number | boolean | string>
  >({});

  const [msgApi, contextHolder] = message.useMessage();

  const load = async () => {
    const [pr, sch, ds, st] = await Promise.all([
      listConfigProfiles(),
      getConfigSchema(),
      listDifyDatasets(),
      listChunkStrategies(),
    ]);
    setProfiles(pr.profiles);
    setActiveId(pr.active_profile_id);
    setFields(sch.fields);
    setDatasets(ds);
    setStrategies(st.strategies);
  };

  useEffect(() => {
    load().catch((e) => msgApi.error(`加载配置失败：${e?.message ?? e}`));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const fieldMap = useMemo(() => {
    const m = new Map<string, ConfigFieldDef>();
    for (const f of fields) m.set(f.key, f);
    return m;
  }, [fields]);

  /** 用默认值（可基于指定方案）构造初始 configValues。 */
  const buildInit = (base?: ConfigProfile) => {
    const init: Record<string, number | boolean | string> = {};
    for (const f of fields) {
      init[f.key] = base?.config[f.key] ?? (f.default as number | boolean | string);
    }
    return init;
  };

  const openCreate = () => {
    setEditing(null);
    const base = profiles.find((p) => p.id === activeId);
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
        await createConfigProfile(name, configValues);
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
      setActiveId(r.active_profile_id);
      await load();
    } catch (e) {
      msgApi.error(`删除失败：${String(e)}`);
    }
  };

  const setCfg = (key: string, v: number | boolean | string) =>
    setConfigValues((prev) => ({ ...prev, [key]: v }));

  /** 当前选中的切分策略。 */
  const currentStrategy = String(configValues.chunk_strategy || "structure");
  const currentStrategyMeta = strategies.find((s) => s.key === currentStrategy) ?? null;

  /** 当前策略下应显示的配置字段（通用字段 + 该策略专属字段）。 */
  const visibleFields = useMemo(() => {
    const filtered = fields.filter((f) => {
      if (f.key === "dify_dataset_id" || f.key === "chunk_strategy") return false;
      const ss = f.strategies ?? [];
      // 无 strategies 元数据（旧 schema）时视为通用字段，全部显示
      if (!ss.length) return true;
      return ss.includes(currentStrategy);
    });
    return filtered;
  }, [fields, currentStrategy]);

  const activeProfile = profiles.find((p) => p.id === activeId) ?? null;

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      {contextHolder}
      <div>
        <Title level={4} style={{ margin: 0 }}>
          配置中心
        </Title>
        <Paragraph type="secondary" style={{ marginTop: 4, marginBottom: 0 }}>
          上传处理前需先配置好<strong>知识库 ID</strong> 与
          <strong>切分策略</strong>（含全部切分参数），并选择一个配置方案激活。
          上传入库时将使用所选（当前激活）配置方案。
        </Paragraph>
      </div>

      <Card
        size="small"
        title="当前激活配置"
        extra={
          <Button type="primary" onClick={openCreate} disabled={!fields.length}>
            新建配置方案
          </Button>
        }
      >
        {activeProfile ? (
          <Descriptions size="small" column={{ xs: 1, md: 3 }}>
            <Descriptions.Item label="方案名称">
              {activeProfile.name}
            </Descriptions.Item>
            <Descriptions.Item label="知识库 ID">
              {String(activeProfile.config.dify_dataset_id || "（未设置）")}
            </Descriptions.Item>
            <Descriptions.Item label="切分策略">
              {String(activeProfile.config.chunk_strategy || "structure")}
            </Descriptions.Item>
          </Descriptions>
        ) : (
          <Paragraph type="secondary" style={{ margin: 0 }}>
            尚未配置任何方案，请先新建并激活一个配置方案。
          </Paragraph>
        )}
      </Card>

      <Row gutter={[16, 16]}>
        {profiles.map((p) => {
          const isActive = p.id === activeId;
          return (
            <Col xs={24} md={12} lg={8} key={p.id}>
              <Card
                size="small"
                title={
                  <Space>
                    {p.name}
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
                  <Descriptions.Item label="知识库 ID">
                    {String(p.config.dify_dataset_id || "（未设置）")}
                  </Descriptions.Item>
                  <Descriptions.Item label="切分策略">
                    {String(p.config.chunk_strategy || "structure")}
                  </Descriptions.Item>
                  <Descriptions.Item label="目标字符数">
                    {String(p.config.chunk_target_chars ?? "-")}
                  </Descriptions.Item>
                  <Descriptions.Item label="硬上限">
                    {String(p.config.chunk_hard_limit ?? "-")}
                  </Descriptions.Item>
                  <Descriptions.Item label="更新时间">
                    {p.updated_at}
                  </Descriptions.Item>
                </Descriptions>
              </Card>
            </Col>
          );
        })}
      </Row>

      <Modal
        title={editing ? `编辑配置方案「${editing.name}」` : "新建配置方案"}
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
            {/* 知识库 ID */}
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
