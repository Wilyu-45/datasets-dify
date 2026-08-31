/**
 * 文档元数据页（★ 2026-08-31 新增：以 Dify 库内文档清单为准）。
 *
 * 文档列表直接来自所选 Dify 知识库（不依赖 manifest 台账）：
 * - 台账里已删除 / 陈旧的记录不会再出现（修复按陈旧 dify_doc_id 推送报 404）；
 * - 知识库里存在但台账没有的文档（后续数据库迁移场景）同样可以填写元数据。
 *
 * 流程：
 *   ① 选择知识库（缺省当前配置的目标知识库）
 *   ② 文档列表（Dify 分页拉全，本地搜索）→ 行上「填写元数据」
 *   ③ 抽屉编辑 doc_metadata 表全部 11 个字段（Dify 已有值兜底回填）→
 *      「保存」（只落库）或「保存并导入 Dify」（顺手推送这一篇）
 *   ④ 卡片右上「导入元数据到 Dify」：知识库全部文档批量推送（50 篇/批）
 */
import {
  Alert,
  Button,
  Card,
  Drawer,
  Empty,
  Form,
  Input,
  InputNumber,
  message,
  Modal,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { CloudUploadOutlined, ReloadOutlined } from "@ant-design/icons";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  getDifyConfig,
  getDocMetadata,
  listDifyDatasets,
  listDifyDocuments,
  listDocMetadata,
  saveDocMetadata,
  syncDifyMetadata,
  type DifyDatasetItem,
  type DifyDocumentItem,
  type DocMetadataFields,
} from "../api/client";

const { Title, Paragraph, Text } = Typography;

/** doc_metadata 表 11 个字段的表单字段名（与后端一一对应） */
const META_FORM_FIELDS: (keyof DocMetadataFields)[] = [
  "doc_type_primary",
  "doc_type_secondary",
  "topic_primary",
  "topic_secondary",
  "core_summary",
  "entity_label",
  "attribute_label",
  "applicable_scenarios",
  "effective_date",
  "priority",
  "status",
];

/** 拉取知识库全部文档（每页 100，返回不足一页即止；API 侧无 total 暴露） */
async function fetchAllDocuments(datasetId?: string): Promise<DifyDocumentItem[]> {
  const out: DifyDocumentItem[] = [];
  for (let page = 1; page <= 20; page++) {
    const items = await listDifyDocuments({ page, limit: 100, dataset_id: datasetId });
    out.push(...items);
    if (items.length < 100) break;
  }
  return out;
}

export default function MetadataPage() {
  // ---- 知识库选择（缺省当前配置）----
  const [datasets, setDatasets] = useState<DifyDatasetItem[]>([]);
  const [datasetId, setDatasetId] = useState<string>();
  const [datasetsLoading, setDatasetsLoading] = useState(false);

  // ---- 文档列表（来自 Dify）----
  const [docs, setDocs] = useState<DifyDocumentItem[]>([]);
  const [docsLoading, setDocsLoading] = useState(false);
  const [keyword, setKeyword] = useState("");

  // ---- 本地已填元数据（doc_metadata 表，按文档名索引）----
  const [metaRows, setMetaRows] = useState<Record<string, DocMetadataFields>>({});

  // ---- 填写抽屉 ----
  const [form] = Form.useForm();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerDoc, setDrawerDoc] = useState<DifyDocumentItem | null>(null);
  const [drawerLoading, setDrawerLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [syncAllLoading, setSyncAllLoading] = useState(false);

  const refreshMetaRows = useCallback(() => {
    listDocMetadata()
      .then((r) => setMetaRows(r.rows ?? {}))
      .catch(() => setMetaRows({}));
  }, []);

  const loadDatasets = useCallback(() => {
    setDatasetsLoading(true);
    Promise.all([listDifyDatasets(), getDifyConfig()])
      .then(([ds, cfg]) => {
        setDatasets(ds);
        // 缺省选中后端当前配置的目标知识库
        setDatasetId((prev) => prev ?? (cfg.dataset_id || undefined));
      })
      .catch((e) => message.error(`知识库列表加载失败：${(e as Error).message}`))
      .finally(() => setDatasetsLoading(false));
  }, []);

  const loadDocs = useCallback((dsId?: string) => {
    setDocsLoading(true);
    fetchAllDocuments(dsId)
      .then(setDocs)
      .catch((e) => {
        setDocs([]);
        message.error(`文档列表加载失败：${(e as Error).message}`);
      })
      .finally(() => setDocsLoading(false));
  }, []);

  useEffect(() => {
    loadDatasets();
    refreshMetaRows();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 切换知识库后重拉文档
  useEffect(() => {
    if (datasetId) loadDocs(datasetId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [datasetId]);

  const filteredDocs = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    if (!kw) return docs;
    return docs.filter((d) => d.name.toLowerCase().includes(kw));
  }, [docs, keyword]);

  /** 某文档当前已填的元数据（doc_metadata 行 + Dify 已写入值，本地行优先） */
  const mergedMetaOf = useCallback(
    (doc: DifyDocumentItem): DocMetadataFields => {
      const merged: DocMetadataFields = {};
      // Dify 已写入的值兜底（迁移后本地表为空时仍能看到已推送的元数据）
      for (const m of doc.metadata ?? []) {
        if (META_FORM_FIELDS.includes(m.name as keyof DocMetadataFields)) {
          (merged as Record<string, unknown>)[m.name] = m.value;
        }
      }
      // 本地 doc_metadata 行优先（最新编辑意图）
      const local = metaRows[doc.name];
      if (local) Object.assign(merged, local);
      return merged;
    },
    [metaRows]
  );

  // ---- 填写元数据 ----
  const openDrawer = (doc: DifyDocumentItem) => {
    setDrawerDoc(doc);
    setDrawerOpen(true);
    setDrawerLoading(true);
    form.resetFields();
    getDocMetadata(doc.name)
      .then((local) => {
        // 回填：Dify 已有值兜底 + 本地行覆盖（本地编辑意图优先）；null 转 undefined 防受控告警
        const merged = { ...mergedMetaOf(doc), ...local } as DocMetadataFields;
        const normalized = Object.fromEntries(
          META_FORM_FIELDS.map((k) => [k, (merged[k] ?? undefined) as never])
        );
        form.setFieldsValue(normalized);
      })
      .catch((e) => message.error(`元数据加载失败：${(e as Error).message}`))
      .finally(() => setDrawerLoading(false));
  };

  const saveMeta = async (andSync: boolean) => {
    if (!drawerDoc) return;
    let values: Record<string, unknown>;
    try {
      values = await form.validateFields();
    } catch {
      return; // 校验失败：表单已标红
    }
    setSaving(true);
    try {
      // 表单字段名与 DocMetadataFields 一一对应；空串统一转 null（后端以 null 清空字段）
      const fields = { ...values } as DocMetadataFields;
      META_FORM_FIELDS.forEach((k) => {
        const v = fields[k];
        if (v == null || v === "") fields[k] = null;
      });
      await saveDocMetadata(drawerDoc.name, fields);
      message.success("元数据已保存");
      refreshMetaRows();
      if (andSync) {
        setSyncing(true);
        try {
          const r = await syncDifyMetadata([drawerDoc.name], datasetId);
          if (r.synced > 0 && r.errors === 0) {
            message.success(`已导入 Dify（${r.synced} 篇）`);
          } else {
            message.warning(r.message || "未写入 Dify（该文档可能没有可推送的元数据）");
          }
        } catch (e) {
          message.error(`导入 Dify 失败：${(e as Error).message}`);
        } finally {
          setSyncing(false);
        }
      }
      setDrawerOpen(false);
    } catch (e) {
      message.error(`保存失败：${(e as Error).message || "未知错误"}`);
    } finally {
      setSaving(false);
    }
  };

  // ---- 全量导入 ----
  const handleSyncAll = () => {
    Modal.confirm({
      title: "导入元数据到 Dify",
      content:
        "将以该知识库内的文档清单为准，把每篇文档的元数据（本页保存的字段 + 清单历史填写列）批量写入，可重复执行（覆盖旧值）；缺失的元数据字段会自动创建。是否继续？",
      okText: "导入",
      cancelText: "取消",
      onOk: async () => {
        setSyncAllLoading(true);
        try {
          const r = await syncDifyMetadata(undefined, datasetId);
          if (r.errors > 0) {
            message.warning(
              `已导入 ${r.synced}/${r.total} 篇，${r.errors} 篇失败（可能已被删除，详见后端日志）`
            );
          } else if (r.synced === 0) {
            message.info(r.message || "没有可导入的元数据：先在行上「填写元数据」");
          } else {
            message.success(`已导入 ${r.synced} 篇文档的元数据`);
          }
          loadDocs(datasetId);
        } catch (e) {
          message.error(`导入失败：${(e as Error).message}`);
        } finally {
          setSyncAllLoading(false);
        }
      },
    });
  };

  // ---- 表格 ----
  const columns: ColumnsType<DifyDocumentItem> = [
    {
      title: "文档名",
      dataIndex: "name",
      ellipsis: true,
      render: (name: string, d) => (
        <Space direction="vertical" size={0}>
          <Text strong ellipsis style={{ maxWidth: 320 }}>
            {name}
          </Text>
          <Tooltip title={d.id}>
            <Text type="secondary" copyable style={{ fontSize: 12 }}>
              {d.id}
            </Text>
          </Tooltip>
        </Space>
      ),
    },
    {
      title: "索引状态",
      dataIndex: "indexing_status",
      width: 110,
      render: (s: string) =>
        s === "completed" ? <Tag color="green">completed</Tag> : <Tag color="gold">{s}</Tag>,
    },
    {
      title: "字数",
      dataIndex: "word_count",
      width: 100,
      align: "right",
      render: (v: number | null | undefined) => (v ?? 0).toLocaleString(),
    },
    {
      title: "元数据",
      key: "meta",
      width: 100,
      render: (_, d) => {
        const n = Object.values(mergedMetaOf(d)).filter(
          (v) => v != null && v !== ""
        ).length;
        const difyN = d.metadata?.length ?? 0;
        return n > 0 ? (
          <Tooltip title={`本地已填 ${n} 项${difyN ? `，Dify 已写入 ${difyN} 项` : ""}`}>
            <Tag color="blue">{n} 项</Tag>
          </Tooltip>
        ) : difyN > 0 ? (
          <Tooltip title={`Dify 已写入 ${difyN} 项（本地未填写）`}>
            <Tag color="purple">{difyN} 项</Tag>
          </Tooltip>
        ) : (
          <Text type="secondary">—</Text>
        );
      },
    },
    {
      title: "操作",
      key: "action",
      width: 110,
      render: (_, d) => (
        <Button type="link" size="small" onClick={() => openDrawer(d)}>
          填写元数据
        </Button>
      ),
    },
  ];

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <div>
        <Title level={4} style={{ margin: 0 }}>
          文档元数据
        </Title>
        <Paragraph type="secondary" style={{ marginTop: 4, marginBottom: 0 }}>
          以 <strong>Dify 知识库内的文档清单</strong>为准（不依赖台账数据库，迁移后仍可用）：
          填写元数据 → 「保存」落库，或「保存并导入 Dify」顺手推送；
          也可一键把全部文档的元数据批量导入知识库。
        </Paragraph>
      </div>

      <Card size="small" title="① 选择知识库">
        <Space wrap>
          <Text strong>目标知识库：</Text>
          <Select
            style={{ minWidth: 360 }}
            placeholder={datasetsLoading ? "知识库加载中..." : "请选择知识库"}
            value={datasetId}
            onChange={setDatasetId}
            loading={datasetsLoading}
            showSearch
            optionFilterProp="label"
            options={datasets.map((d) => ({
              value: d.id,
              label: `${d.name}（${d.document_count} 文档）`,
            }))}
          />
          <Button icon={<ReloadOutlined />} onClick={loadDatasets} loading={datasetsLoading}>
            刷新知识库
          </Button>
        </Space>
      </Card>

      <Card
        size="small"
        title={`② 知识库文档（${filteredDocs.length} 篇）`}
        extra={
          <Space>
            <Button
              size="small"
              type="primary"
              ghost
              icon={<CloudUploadOutlined />}
              loading={syncAllLoading}
              disabled={!datasetId && !docs.length}
              onClick={handleSyncAll}
            >
              导入元数据到 Dify
            </Button>
            <Button
              size="small"
              icon={<ReloadOutlined />}
              loading={docsLoading}
              onClick={() => loadDocs(datasetId)}
            >
              刷新
            </Button>
          </Space>
        }
      >
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <Input.Search
            allowClear
            placeholder="按文档名搜索（本地过滤）"
            onSearch={setKeyword}
            onChange={(e) => !e.target.value && setKeyword("")}
            style={{ maxWidth: 360 }}
          />
          {docs.length === 0 && !docsLoading ? (
            <Empty description="该知识库暂无文档（或未选择知识库）" />
          ) : (
            <Table
              size="small"
              rowKey="id"
              loading={docsLoading}
              columns={columns}
              dataSource={filteredDocs}
              pagination={filteredDocs.length > 20 ? { pageSize: 20 } : false}
            />
          )}
          <Text type="secondary" style={{ fontSize: 12 }}>
            清单不依赖数据库台账：知识库里已删除的文档不会再出现；库里存在但台账没有的文档同样可填写（适配后续数据库迁移）。
          </Text>
        </Space>
      </Card>

      {/* ③ 填写元数据抽屉：doc_metadata 表全部 11 个字段 */}
      <Drawer
        title={`填写元数据：${drawerDoc?.name ?? ""}`}
        width={520}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        footer={
          <Space style={{ display: "flex", justifyContent: "flex-end" }}>
            <Button onClick={() => setDrawerOpen(false)}>取消</Button>
            <Button loading={syncing} disabled={saving} onClick={() => saveMeta(true)}>
              保存并导入 Dify
            </Button>
            <Button
              type="primary"
              loading={saving}
              disabled={syncing}
              onClick={() => saveMeta(false)}
            >
              保存
            </Button>
          </Space>
        }
      >
        <Spin spinning={drawerLoading}>
          <Space direction="vertical" size="middle" style={{ width: "100%" }}>
            <Alert
              type="info"
              showIcon
              message="保存到 doc_metadata 表；「保存并导入 Dify」同时把这篇文档的元数据写入知识库"
              description="表单会回填 Dify 已写入的值（数据库迁移后本地为空也能看到已推送的元数据）。清单历史填写列（分类/关键词等）导入时一并推送。"
            />
            <Form form={form} layout="vertical" requiredMark={false}>
              <Form.Item name="doc_type_primary" label="类型-一级">
                <Input placeholder="如：法律法规 / 操作规范" />
              </Form.Item>
              <Form.Item name="doc_type_secondary" label="类型-二级">
                <Input placeholder="如：国家标准 / 院内制度" />
              </Form.Item>
              <Form.Item name="topic_primary" label="主题-一级">
                <Input />
              </Form.Item>
              <Form.Item name="topic_secondary" label="主题-二级">
                <Input />
              </Form.Item>
              <Form.Item name="core_summary" label="核心内容摘要">
                <Input.TextArea rows={3} placeholder="文档核心内容的一句话 / 一段话摘要" />
              </Form.Item>
              <Form.Item name="entity_label" label="实体标签">
                <Input placeholder="如：医院 / 疾病 / 药品，多个用逗号分隔" />
              </Form.Item>
              <Form.Item name="attribute_label" label="属性标签">
                <Input placeholder="如：高优先级 / 外科，多个用逗号分隔" />
              </Form.Item>
              <Form.Item name="applicable_scenarios" label="适用科室">
                <Input placeholder="如：全院 / 呼吸内科" />
              </Form.Item>
              <Form.Item name="effective_date" label="生效日期">
                <Input placeholder="如：2009-06-01 / 无" />
              </Form.Item>
              <Form.Item name="priority" label="优先级（数字，越大越优先）">
                <InputNumber style={{ width: "100%" }} placeholder="如：1 / 0.8" />
              </Form.Item>
              <Form.Item name="status" label="状态">
                <Input placeholder="如：现行 / 废止" />
              </Form.Item>
            </Form>
          </Space>
        </Spin>
      </Drawer>
    </Space>
  );
}
