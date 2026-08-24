import {
  Alert,
  Badge,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Input,
  Row,
  Space,
  Spin,
  Statistic,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  CopyOutlined,
  EditOutlined,
  FileSearchOutlined,
  PictureOutlined,
  ReloadOutlined,
  SaveOutlined,
  SearchOutlined,
  UndoOutlined,
} from "@ant-design/icons";
import { useEffect, useMemo, useState } from "react";
import {
  getDifyConfig,
  listDifyDocuments,
  listDifySegments,
  updateDifySegment,
  type DifyConfigInfo,
  type DifyDocumentItem,
  type DifySegmentItem,
} from "../api/client";

const { Title, Text, Paragraph } = Typography;
const { TextArea } = Input;

const STATUS_COLORS: Record<string, string> = {
  completed: "green",
  waiting: "gold",
  indexing: "blue",
  error: "red",
};

export default function VerifyPage() {
  const [config, setConfig] = useState<DifyConfigInfo | null>(null);
  const [documents, setDocuments] = useState<DifyDocumentItem[]>([]);
  const [docLoading, setDocLoading] = useState(false);
  const [docKeyword, setDocKeyword] = useState("");
  const [selectedDocId, setSelectedDocId] = useState<string | null>(null);

  const [segments, setSegments] = useState<DifySegmentItem[]>([]);
  const [segLoading, setSegLoading] = useState(false);
  const [segKeyword, setSegKeyword] = useState("");
  const [selectedSegId, setSelectedSegId] = useState<string | null>(null);

  // 编辑态
  const [editContent, setEditContent] = useState<string>("");
  const [editEnabled, setEditEnabled] = useState<boolean>(true);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);

  const [msgApi, ctxHolder] = message.useMessage();

  // 加载配置
  useEffect(() => {
    getDifyConfig()
      .then(setConfig)
      .catch((e) => msgApi.error(`加载 Dify 配置失败：${(e as Error).message}`));
  }, [msgApi]);

  // 加载文档列表
  const loadDocuments = async (kw = docKeyword) => {
    setDocLoading(true);
    try {
      const docs = await listDifyDocuments({ keyword: kw || undefined, limit: 50 });
      setDocuments(docs);
      // 若之前选中的文档不存在了，清空选中
      if (selectedDocId && !docs.find((d) => d.id === selectedDocId)) {
        setSelectedDocId(null);
        setSegments([]);
        setSelectedSegId(null);
      }
    } catch (e) {
      msgApi.error(`加载 Dify 文档失败：${(e as Error).message}`);
    } finally {
      setDocLoading(false);
    }
  };

  useEffect(() => {
    loadDocuments("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 选中文档后加载分段
  useEffect(() => {
    if (!selectedDocId) {
      setSegments([]);
      setSelectedSegId(null);
      return;
    }
    setSegLoading(true);
    listDifySegments(selectedDocId, { keyword: segKeyword || undefined })
      .then((items) => {
        setSegments(items);
        if (items.length > 0 && !items.find((s) => s.id === selectedSegId)) {
          setSelectedSegId(items[0].id);
        }
      })
      .catch((e) => msgApi.error(`加载分段失败：${(e as Error).message}`))
      .finally(() => setSegLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedDocId]);

  // 选中分段后 → 同步到编辑态
  const activeSegment = useMemo(
    () => segments.find((s) => s.id === selectedSegId) ?? null,
    [segments, selectedSegId]
  );

  useEffect(() => {
    if (activeSegment) {
      setEditContent(activeSegment.content);
      setEditEnabled(activeSegment.enabled);
      setDirty(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSegment?.id]);

  const onSave = async () => {
    if (!selectedDocId || !activeSegment) return;
    setSaving(true);
    try {
      await updateDifySegment(selectedDocId, activeSegment.id, {
        content: editContent,
        enabled: editEnabled,
      });
      msgApi.success(`已保存分段 #${activeSegment.position}（${activeSegment.id}）`);
      // 刷新当前分段列表
      const items = await listDifySegments(selectedDocId, {
        keyword: segKeyword || undefined,
      });
      setSegments(items);
      setDirty(false);
    } catch (e) {
      msgApi.error(`保存失败：${(e as Error).message}`);
    } finally {
      setSaving(false);
    }
  };

  const onReset = () => {
    if (!activeSegment) return;
    setEditContent(activeSegment.content);
    setEditEnabled(activeSegment.enabled);
    setDirty(false);
  };

  const filteredDocs = useMemo(() => {
    if (!docKeyword) return documents;
    const kw = docKeyword.toLowerCase();
    return documents.filter(
      (d) =>
        d.name.toLowerCase().includes(kw) ||
        d.id.toLowerCase().includes(kw) ||
        d.indexing_status.toLowerCase().includes(kw)
    );
  }, [documents, docKeyword]);

  const filteredSegs = useMemo(() => {
    if (!segKeyword) return segments;
    const kw = segKeyword.toLowerCase();
    return segments.filter(
      (s) =>
        s.content.toLowerCase().includes(kw) ||
        s.id.toLowerCase().includes(kw)
    );
  }, [segments, segKeyword]);

  // 未配置 Dify
  if (config && !config.has_api_key) {
    return (
      <Card>
        {ctxHolder}
        <Alert
          type="warning"
          showIcon
          message="Dify API Key 未配置"
          description={
            <Space direction="vertical">
              <Text>请在 backend/.env 设置 RAG_DIFY_API_KEY 后重启服务。</Text>
              <Text type="secondary" style={{ fontSize: 12 }}>
                路径：<code>backend/.env</code> → <code>RAG_DIFY_API_KEY=dataset-xxx</code>
              </Text>
            </Space>
          }
        />
      </Card>
    );
  }

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      {ctxHolder}
      <div>
        <Title level={4} style={{ margin: 0 }}>
          步骤 3.5 · 人工校验
        </Title>
        <Paragraph type="secondary" style={{ marginTop: 4, marginBottom: 0 }}>
          拉取 Dify 数据集中的所有文档 → 选中一个文档查看其所有分段 →
          编辑分段 <code>content</code> / <code>enabled</code> → 保存后会调
          <code> POST /datasets/{`{id}`}/documents/{`{doc}`}/segments/{`{seg}`}</code> 写回 Dify。
        </Paragraph>
      </div>

      {/* 三栏布局 */}
      <Row gutter={12}>
        {/* ====== 左栏：文档列表 ====== */}
        <Col xs={24} lg={6}>
          <Card
            size="small"
            title={
              <Space>
                <FileSearchOutlined />
                <span>Dify 文档</span>
                <Badge count={documents.length} showZero color="#1677ff" />
              </Space>
            }
            extra={
              <Tooltip title="刷新文档列表">
                <Button
                  size="small"
                  icon={<ReloadOutlined />}
                  loading={docLoading}
                  onClick={() => loadDocuments(docKeyword)}
                />
              </Tooltip>
            }
          >
            <Input
              size="small"
              placeholder="搜索文档名 / ID / 状态"
              prefix={<SearchOutlined />}
              allowClear
              value={docKeyword}
              onChange={(e) => setDocKeyword(e.target.value)}
              onPressEnter={() => loadDocuments(docKeyword)}
              style={{ marginBottom: 8 }}
            />
            <Spin spinning={docLoading} size="small">
              {filteredDocs.length === 0 ? (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description={docLoading ? "加载中…" : "暂无文档"}
                  style={{ padding: 24 }}
                />
              ) : (
                <div
                  style={{
                    maxHeight: "calc(100vh - 360px)",
                    overflowY: "auto",
                  }}
                >
                  {filteredDocs.map((d) => {
                    const isActive = d.id === selectedDocId;
                    return (
                      <Card
                        key={d.id}
                        size="small"
                        hoverable
                        onClick={() => setSelectedDocId(d.id)}
                        style={{
                          marginBottom: 6,
                          borderColor: isActive ? "#1677ff" : undefined,
                          background: isActive ? "#e6f4ff" : undefined,
                        }}
                      >
                        <Space
                          direction="vertical"
                          size={2}
                          style={{ width: "100%" }}
                        >
                          <Space style={{ width: "100%", justifyContent: "space-between" }}>
                            <Text
                              strong
                              style={{
                                fontSize: 13,
                                whiteSpace: "nowrap",
                                overflow: "hidden",
                                textOverflow: "ellipsis",
                                maxWidth: 160,
                              }}
                              title={d.name}
                            >
                              {d.name || <Text type="secondary">(无标题)</Text>}
                            </Text>
                            <Tag
                              color={STATUS_COLORS[d.indexing_status] ?? "default"}
                              style={{ fontSize: 11, marginInlineEnd: 0 }}
                            >
                              {d.indexing_status}
                            </Tag>
                          </Space>
                          <Space size={4} wrap>
                            {!d.enabled && <Tag color="red">禁用</Tag>}
                            {d.word_count != null && (
                              <Tag color="blue">{d.word_count} 字</Tag>
                            )}
                            <code style={{ fontSize: 10, color: "#999" }}>
                              {d.id.slice(0, 12)}…
                            </code>
                          </Space>
                        </Space>
                      </Card>
                    );
                  })}
                </div>
              )}
            </Spin>
          </Card>
        </Col>

        {/* ====== 中栏：分段列表 ====== */}
        <Col xs={24} lg={7}>
          <Card
            size="small"
            title={
              <Space>
                <span>分段列表</span>
                <Badge count={segments.length} showZero color="#52c41a" />
              </Space>
            }
            extra={
              <Tooltip title="刷新分段">
                <Button
                  size="small"
                  icon={<ReloadOutlined />}
                  loading={segLoading}
                  onClick={() => {
                    if (selectedDocId) {
                      setSegLoading(true);
                      listDifySegments(selectedDocId, {
                        keyword: segKeyword || undefined,
                      })
                        .then(setSegments)
                        .catch((e) =>
                          msgApi.error(
                            `刷新分段失败：${(e as Error).message}`
                          )
                        )
                        .finally(() => setSegLoading(false));
                    }
                  }}
                />
              </Tooltip>
            }
          >
            {!selectedDocId ? (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="请先在左侧选中一个文档"
                style={{ padding: 24 }}
              />
            ) : (
              <>
                <Input
                  size="small"
                  placeholder="搜索分段内容 / ID"
                  prefix={<SearchOutlined />}
                  allowClear
                  value={segKeyword}
                  onChange={(e) => setSegKeyword(e.target.value)}
                  style={{ marginBottom: 8 }}
                />
                <Spin spinning={segLoading} size="small">
                  {filteredSegs.length === 0 ? (
                    <Empty
                      image={Empty.PRESENTED_IMAGE_SIMPLE}
                      description={segLoading ? "加载中…" : "该文档暂无分段"}
                      style={{ padding: 24 }}
                    />
                  ) : (
                    <div
                      style={{
                        maxHeight: "calc(100vh - 360px)",
                        overflowY: "auto",
                      }}
                    >
                      {filteredSegs.map((s) => {
                        const isActive = s.id === selectedSegId;
                        return (
                          <Card
                            key={s.id}
                            size="small"
                            hoverable
                            onClick={() => setSelectedSegId(s.id)}
                            style={{
                              marginBottom: 6,
                              borderColor: isActive ? "#52c41a" : undefined,
                              background: isActive ? "#f6ffed" : undefined,
                            }}
                          >
                            <Space
                              direction="vertical"
                              size={2}
                              style={{ width: "100%" }}
                            >
                              <Space
                                style={{
                                  width: "100%",
                                  justifyContent: "space-between",
                                }}
                              >
                                <Space size={4}>
                                  <Tag color="default" style={{ fontSize: 11 }}>
                                    #{s.position}
                                  </Tag>
                                  {!s.enabled && <Tag color="red">禁用</Tag>}
                                  {s.attachments.length > 0 && (
                                    <Tag
                                      color="purple"
                                      icon={<PictureOutlined />}
                                      style={{ fontSize: 11 }}
                                    >
                                      {s.attachments.length}
                                    </Tag>
                                  )}
                                </Space>
                                <Text type="secondary" style={{ fontSize: 11 }}>
                                  {s.word_count} 字
                                </Text>
                              </Space>
                              <div
                                style={{
                                  fontSize: 12,
                                  color: "#555",
                                  display: "-webkit-box",
                                  WebkitLineClamp: 3,
                                  WebkitBoxOrient: "vertical",
                                  overflow: "hidden",
                                  textOverflow: "ellipsis",
                                  maxHeight: 60,
                                }}
                              >
                                {s.content.slice(0, 200) || (
                                  <Text type="secondary">(空内容)</Text>
                                )}
                              </div>
                            </Space>
                          </Card>
                        );
                      })}
                    </div>
                  )}
                </Spin>
              </>
            )}
          </Card>
        </Col>

        {/* ====== 右栏：分段详情（编辑） ====== */}
        <Col xs={24} lg={11}>
          <Card
            size="small"
            title={
              <Space>
                <EditOutlined />
                <span>分段详情 / 编辑</span>
                {dirty && <Tag color="orange">未保存</Tag>}
              </Space>
            }
            extra={
              <Space>
                <Tooltip title="放弃本次修改">
                  <Button
                    size="small"
                    icon={<UndoOutlined />}
                    onClick={onReset}
                    disabled={!dirty || saving}
                  >
                    重置
                  </Button>
                </Tooltip>
                <Button
                  size="small"
                  type="primary"
                  icon={<SaveOutlined />}
                  loading={saving}
                  disabled={!dirty || !activeSegment}
                  onClick={onSave}
                >
                  保存到 Dify
                </Button>
              </Space>
            }
          >
            {!activeSegment ? (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="请在中间栏选中一个分段"
                style={{ padding: 60 }}
              />
            ) : (
              <Tabs
                size="small"
                defaultActiveKey="edit"
                items={[
                  {
                    key: "edit",
                    label: "编辑",
                    children: (
                      <Space
                        direction="vertical"
                        size="small"
                        style={{ width: "100%" }}
                      >
                        <Descriptions
                          size="small"
                          column={2 as const}
                          items={[
                            { key: "id", label: "Segment ID", children: <code>{activeSegment.id}</code> },
                            { key: "doc", label: "Document ID", children: <code>{activeSegment.document_id}</code> },
                            { key: "pos", label: "Position", children: <Tag>#{activeSegment.position}</Tag> },
                            { key: "words", label: "字数", children: <Tag color="blue">{activeSegment.word_count}</Tag> },
                            { key: "tokens", label: "Tokens", children: <Tag>{activeSegment.tokens}</Tag> },
                            { key: "status", label: "Status", children: <Tag color={STATUS_COLORS[activeSegment.status] ?? "default"}>{activeSegment.status}</Tag> },
                          ]}
                        />
                        <Space>
                          <Text>启用此分段：</Text>
                          <Switch
                            checked={editEnabled}
                            onChange={(v) => {
                              setEditEnabled(v);
                              setDirty(true);
                            }}
                          />
                        </Space>
                        <div>
                          <Text strong style={{ fontSize: 12 }}>
                            Content（{editContent.length} 字 / 原始 {activeSegment.content.length} 字）
                          </Text>
                          <TextArea
                            value={editContent}
                            onChange={(e) => {
                              setEditContent(e.target.value);
                              setDirty(true);
                            }}
                            autoSize={{ minRows: 12, maxRows: 28 }}
                            style={{
                              fontFamily:
                                "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
                              fontSize: 12,
                              marginTop: 4,
                            }}
                            placeholder="分段 Markdown 内容（可包含 ![](url) 图片引用）"
                          />
                        </div>
                      </Space>
                    ),
                  },
                  {
                    key: "preview",
                    label: "渲染预览",
                    children: (
                      <div
                        style={{
                          maxHeight: "calc(100vh - 360px)",
                          overflow: "auto",
                          padding: 12,
                          background: "#fafafa",
                          border: "1px solid #f0f0f0",
                          borderRadius: 4,
                          fontSize: 13,
                          lineHeight: 1.7,
                          whiteSpace: "pre-wrap",
                          wordBreak: "break-word",
                        }}
                      >
                        {editContent || (
                          <Text type="secondary">(空内容)</Text>
                        )}
                      </div>
                    ),
                  },
                  {
                    key: "attachments",
                    label: `图片附件 (${activeSegment.attachments.length})`,
                    children:
                      activeSegment.attachments.length === 0 ? (
                        <Empty
                          image={Empty.PRESENTED_IMAGE_SIMPLE}
                          description="此分段无图片附件"
                          style={{ padding: 24 }}
                        />
                      ) : (
                        <Row gutter={[8, 8]}>
                          {activeSegment.attachments.map((a) => (
                            <Col key={a.id} xs={12} md={8}>
                              <Card
                                size="small"
                                hoverable
                                onClick={() => {
                                  if (a.source_url || a.url) {
                                    window.open(
                                      a.source_url ?? a.url!,
                                      "_blank"
                                    );
                                  }
                                }}
                              >
                                {a.source_url || a.url ? (
                                  <img
                                    src={a.source_url ?? a.url}
                                    alt={a.name}
                                    style={{
                                      maxWidth: "100%",
                                      maxHeight: 120,
                                      objectFit: "contain",
                                    }}
                                  />
                                ) : (
                                  <Text type="secondary">无 URL</Text>
                                )}
                                <div style={{ fontSize: 11, marginTop: 4 }}>
                                  {a.name ?? a.id}
                                </div>
                              </Card>
                            </Col>
                          ))}
                        </Row>
                      ),
                  },
                  {
                    key: "meta",
                    label: "元数据",
                    children: (
                      <Space
                        direction="vertical"
                        size="small"
                        style={{ width: "100%" }}
                      >
                        <Paragraph style={{ marginBottom: 0, fontSize: 12 }}>
                          <Text type="secondary">Segment ID：</Text>
                          <code>{activeSegment.id}</code>
                        </Paragraph>
                        <Paragraph style={{ marginBottom: 0, fontSize: 12 }}>
                          <Text type="secondary">Document ID：</Text>
                          <code>{activeSegment.document_id}</code>
                        </Paragraph>
                        <Paragraph style={{ marginBottom: 0, fontSize: 12 }}>
                          <Text type="secondary">Position：</Text>#
                          {activeSegment.position}
                        </Paragraph>
                        <Paragraph style={{ marginBottom: 0, fontSize: 12 }}>
                          <Text type="secondary">字数：</Text>
                          <Tag>{activeSegment.word_count}</Tag>
                        </Paragraph>
                        <Paragraph style={{ marginBottom: 0, fontSize: 12 }}>
                          <Text type="secondary">Tokens：</Text>
                          <Tag>{activeSegment.tokens}</Tag>
                        </Paragraph>
                        <Paragraph style={{ marginBottom: 0, fontSize: 12 }}>
                          <Text type="secondary">Status：</Text>
                          <Tag
                            color={
                              STATUS_COLORS[activeSegment.status] ?? "default"
                            }
                          >
                            {activeSegment.status}
                          </Tag>
                        </Paragraph>
                        <Paragraph style={{ marginBottom: 0, fontSize: 12 }}>
                          <Text type="secondary">启用：</Text>
                          {activeSegment.enabled ? (
                            <Tag color="green" icon={<CheckCircleOutlined />}>
                              是
                            </Tag>
                          ) : (
                            <Tag color="red" icon={<CloseCircleOutlined />}>
                              否
                            </Tag>
                          )}
                        </Paragraph>
                        <Paragraph style={{ marginBottom: 0, fontSize: 12 }}>
                          <Text type="secondary">附件数：</Text>
                          <Tag color="purple">{activeSegment.attachments.length}</Tag>
                        </Paragraph>
                        <Space wrap>
                          <Tag
                            color="blue"
                            style={{ cursor: "pointer" }}
                            onClick={() => {
                              navigator.clipboard?.writeText(activeSegment.content);
                              msgApi.success("已复制原始 content 到剪贴板");
                            }}
                          >
                            <CopyOutlined /> 复制原始 content
                          </Tag>
                          {dirty && (
                            <Tag color="orange">⚠ 有未保存的修改</Tag>
                          )}
                        </Space>
                      </Space>
                    ),
                  },
                ]}
              />
            )}
          </Card>
        </Col>
      </Row>

      {/* 底部统计 */}
      <Card size="small" type="inner">
        <Row gutter={16}>
          <Col xs={6}>
            <Statistic title="Dify 文档数" value={documents.length} />
          </Col>
          <Col xs={6}>
            <Statistic
              title="当前文档分段数"
              value={segments.length}
              valueStyle={{ color: segments.length > 0 ? "#52c41a" : undefined }}
            />
          </Col>
          <Col xs={6}>
            <Statistic
              title="当前选中分段"
              value={activeSegment ? `#${activeSegment.position}` : "—"}
            />
          </Col>
          <Col xs={6}>
            <Statistic
              title="保存状态"
              value={dirty ? "有未保存修改" : "已同步"}
              valueStyle={{ color: dirty ? "#faad14" : "#52c41a" }}
            />
          </Col>
        </Row>
      </Card>
    </Space>
  );
}
