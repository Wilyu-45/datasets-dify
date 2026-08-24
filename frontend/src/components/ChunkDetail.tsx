import {
  Card,
  Col,
  Empty,
  Row,
  Space,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import {
  EyeOutlined,
  FileTextOutlined,
  PictureOutlined,
  CopyOutlined,
} from "@ant-design/icons";
import { useEffect, useState } from "react";
import {
  listChunkMeta,
  previewChunk,
  type ChunkMeta,
  type ChunkPreview,
} from "../api/client";

const { Text, Paragraph } = Typography;

const TYPE_COLORS: Record<string, string> = {
  cover: "magenta",
  toc: "cyan",
  preface: "geekblue",
  body: "blue",
  appendix: "purple",
  reference: "orange",
  single: "default",
  parent: "green", // ★ 2026-08-24 父-子切分的父块
};

const TYPE_LABELS: Record<string, string> = {
  cover: "封面",
  toc: "目录",
  preface: "前言",
  body: "正文",
  appendix: "附录",
  reference: "参考文献",
  single: "单段",
  parent: "父块",
};

interface Props {
  stem: string | null;
  refreshKey: number;
}

export default function ChunkDetail({ stem, refreshKey }: Props) {
  const [metas, setMetas] = useState<ChunkMeta[]>([]);
  const [metaLoading, setMetaLoading] = useState(false);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [preview, setPreview] = useState<ChunkPreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [msgApi, ctxHolder] = message.useMessage();

  // 加载元数据
  useEffect(() => {
    if (!stem) {
      setMetas([]);
      setActiveId(null);
      setPreview(null);
      return;
    }
    setMetaLoading(true);
    listChunkMeta(stem)
      .then((items) => {
        setMetas(items);
        // 默认选中第一个
        if (items.length > 0) {
          setActiveId(items[0].chunk_id);
        } else {
          setActiveId(null);
          setPreview(null);
        }
      })
      .catch((e) => {
        msgApi.error(`加载 chunks 失败：${(e as Error).message}`);
        setMetas([]);
      })
      .finally(() => setMetaLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stem, refreshKey]);

  // 加载预览
  useEffect(() => {
    if (!stem || !activeId) {
      setPreview(null);
      return;
    }
    setPreviewLoading(true);
    previewChunk(stem, activeId)
      .then((p) => setPreview(p))
      .catch((e) => {
        msgApi.error(`加载预览失败：${(e as Error).message}`);
        setPreview(null);
      })
      .finally(() => setPreviewLoading(false));
  }, [stem, activeId, msgApi]);

  if (!stem) {
    return (
      <Card
        size="small"
        title={
          <Space>
            <FileTextOutlined />
            <span>🔍 Chunk 预览</span>
          </Space>
        }
      >
        <Empty
          description={
            <span style={{ color: "#999" }}>
              请在上方「chunks/ 切分产物」表格里点击 <b>查看 chunks</b> 选中一个文档
            </span>
          }
        />
      </Card>
    );
  }

  const activeMeta = metas.find((m) => m.chunk_id === activeId);

  return (
    <Card
      size="small"
      title={
        <Space>
          <FileTextOutlined />
          <span>🔍 Chunk 预览</span>
          <Tag color="processing">{stem}</Tag>
          <Text type="secondary" style={{ fontSize: 12 }}>
            共 {metas.length} 个 chunk
          </Text>
        </Space>
      }
    >
      {ctxHolder}
      <Row gutter={16}>
        <Col xs={24} lg={10}>
          <Table
            size="small"
            rowKey="chunk_id"
            loading={metaLoading}
            dataSource={metas}
            scroll={{ x: 480, y: 480 }}
            pagination={{ pageSize: 20, showSizeChanger: false }}
            rowClassName={(r) =>
              r.chunk_id === activeId ? "ant-table-row-selected" : ""
            }
            onRow={(record) => ({
              onClick: () => setActiveId(record.chunk_id),
              style: { cursor: "pointer" },
            })}
            columns={[
              {
                title: "ID",
                dataIndex: "chunk_id",
                width: 90,
                render: (v: string) => <code style={{ fontSize: 12 }}>{v}</code>,
              },
              {
                title: "类型",
                dataIndex: "chunk_type",
                width: 80,
                render: (v: string) => (
                  <Tag color={TYPE_COLORS[v] ?? "default"}>
                    {TYPE_LABELS[v] ?? v}
                  </Tag>
                ),
              },
              {
                title: "策略",
                dataIndex: "strategy",
                width: 110,
                render: (v: string | undefined) =>
                  v && v !== "structure" ? (
                    <Tag color="geekblue">{v}</Tag>
                  ) : (
                    <span style={{ color: "#bbb", fontSize: 12 }}>—</span>
                  ),
              },
              {
                title: "标题路径",
                dataIndex: "title_path",
                ellipsis: true,
                render: (v: string, r: ChunkMeta) => (
                  <Tooltip
                    title={
                      <span>
                        {v}
                        {r.is_split ? "（二次切分）" : ""}
                      </span>
                    }
                  >
                    <Space size={4}>
                      {r.is_split && <Tag color="orange">split</Tag>}
                      {r.image_refs.length > 0 && (
                        <Tag color="purple" icon={<PictureOutlined />}>
                          {r.image_refs.length}
                        </Tag>
                      )}
                      <span style={{ fontSize: 12 }}>{v}</span>
                    </Space>
                  </Tooltip>
                ),
              },
              {
                title: "字符数",
                dataIndex: "char_count",
                width: 80,
                render: (v: number) => (
                  <Tag color={v > 1500 ? "red" : v > 1200 ? "orange" : "blue"}>
                    {v}
                  </Tag>
                ),
              },
              {
                title: "操作",
                key: "op",
                width: 60,
                render: (_: unknown, r: ChunkMeta) => (
                  <Tooltip title="预览">
                    <EyeOutlined
                      style={{
                        color: r.chunk_id === activeId ? "#1677ff" : "#999",
                      }}
                    />
                  </Tooltip>
                ),
              },
            ]}
          />
        </Col>
        <Col xs={24} lg={14}>
          {activeMeta && (
            <div style={{ marginBottom: 8 }}>
              <Space wrap>
                <Tag color={TYPE_COLORS[activeMeta.chunk_type] ?? "default"}>
                  {TYPE_LABELS[activeMeta.chunk_type] ?? activeMeta.chunk_type}
                </Tag>
                <Text strong style={{ fontSize: 13 }}>
                  {activeMeta.title_path}
                </Text>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {activeMeta.chunk_id} · {activeMeta.char_count} 字
                  {activeMeta.image_refs.length > 0 &&
                    ` · ${activeMeta.image_refs.length} 张图`}
                </Text>
              </Space>
            </div>
          )}
          {preview ? (
            <PreviewPanel preview={preview} loading={previewLoading} />
          ) : (
            <Empty
              description={
                previewLoading ? "加载中…" : "选中左侧的 chunk 以预览内容"
              }
            />
          )}
        </Col>
      </Row>
    </Card>
  );
}

function PreviewPanel({
  preview,
  loading,
}: {
  preview: ChunkPreview;
  loading: boolean;
}) {
  return (
    <Tabs
      size="small"
      defaultActiveKey="rendered"
      items={[
        {
          key: "rendered",
          label: "渲染预览",
          children: (
            <div
              style={{
                maxHeight: 520,
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
              {loading ? "加载中…" : preview.content}
            </div>
          ),
        },
        {
          key: "raw",
          label: "Markdown 原文",
          children: (
            <div
              style={{
                maxHeight: 520,
                overflow: "auto",
                padding: 12,
                background: "#1e1e1e",
                color: "#d4d4d4",
                borderRadius: 4,
                fontSize: 12,
                fontFamily:
                  "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
              }}
            >
              {preview.content}
            </div>
          ),
        },
        {
          key: "meta",
          label: "元数据",
          children: (
            <Space direction="vertical" size="small" style={{ width: "100%" }}>
              <Paragraph style={{ marginBottom: 0, fontSize: 12 }}>
                <Text type="secondary">文件：</Text>
                <code>{preview.file_name}</code>
              </Paragraph>
              <Paragraph style={{ marginBottom: 0, fontSize: 12 }}>
                <Text type="secondary">chunk_id：</Text>
                <code>{preview.chunk_id}</code>
              </Paragraph>
              <Paragraph style={{ marginBottom: 0, fontSize: 12 }}>
                <Text type="secondary">stem：</Text>
                <code>{preview.stem}</code>
              </Paragraph>
              <Paragraph style={{ marginBottom: 0, fontSize: 12 }}>
                <Text type="secondary">总字符数：</Text>
                <Tag>{preview.content.length}</Tag>
              </Paragraph>
              <Paragraph style={{ marginBottom: 0, fontSize: 12 }}>
                <Text type="secondary">非空行数：</Text>
                <Tag>
                  {
                    preview.content
                      .split("\n")
                      .filter((l) => l.trim().length > 0).length
                  }
                </Tag>
              </Paragraph>
              <Paragraph style={{ marginBottom: 0, fontSize: 12 }}>
                <Text type="secondary">操作：</Text>
                <Space>
                  <Tag
                    color="blue"
                    style={{ cursor: "pointer" }}
                    onClick={() => {
                      navigator.clipboard?.writeText(preview.content);
                      message.success("已复制到剪贴板");
                    }}
                  >
                    <CopyOutlined /> 复制全文
                  </Tag>
                </Space>
              </Paragraph>
            </Space>
          ),
        },
      ]}
    />
  );
}
