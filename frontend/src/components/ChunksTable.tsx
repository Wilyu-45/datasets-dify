import { Card, Table, Tag, Button, Space, Tooltip, Radio } from "antd";
import {
  ReloadOutlined,
  FolderOpenOutlined,
  FileTextOutlined,
  PictureOutlined,
  InfoCircleOutlined,
  EyeOutlined,
} from "@ant-design/icons";
import { useEffect, useState } from "react";
import {
  listChunks,
  listChunkFiles,
  type ChunkSummary,
  type ChunkFile,
} from "../api/client";

function formatBytes(b: number): string {
  if (b < 1024) return `${b} B`;
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
  return `${(b / 1024 / 1024).toFixed(2)} MB`;
}

const KIND_COLORS: Record<string, string> = {
  chunk: "blue",
  image: "purple",
  metadata: "gold",
  other: "default",
};

const KIND_ICONS: Record<string, React.ReactNode> = {
  chunk: <FileTextOutlined />,
  image: <PictureOutlined />,
  metadata: <InfoCircleOutlined />,
  other: <FileTextOutlined />,
};

interface Props {
  refreshKey: number;
  selectedStem: string | null;
  onSelect: (stem: string | null) => void;
}

export default function ChunksTable({ refreshKey, selectedStem, onSelect }: Props) {
  const [data, setData] = useState<ChunkSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState<Record<string, ChunkFile[]>>({});
  const [expandedLoading, setExpandedLoading] = useState<Record<string, boolean>>({});
  const [fileKindFilter, setFileKindFilter] = useState<Record<string, string>>({});

  const load = async () => {
    setLoading(true);
    try {
      const items = await listChunks();
      setData(items);
      // 如果当前选中的 stem 不在最新数据里，清掉选中
      if (selectedStem && !items.find((it) => it.stem === selectedStem)) {
        onSelect(null);
      }
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey]);

  const toggleExpand = async (expandedFlag: boolean, record: ChunkSummary) => {
    if (!expandedFlag) return;
    if (expanded[record.stem]) return;
    setExpandedLoading((m) => ({ ...m, [record.stem]: true }));
    try {
      const files = await listChunkFiles(record.stem);
      setExpanded((m) => ({ ...m, [record.stem]: files }));
    } finally {
      setExpandedLoading((m) => ({ ...m, [record.stem]: false }));
    }
  };

  const filteredFiles = (stem: string): ChunkFile[] => {
    const files = expanded[stem] ?? [];
    const kind = fileKindFilter[stem] ?? "all";
    if (kind === "all") return files;
    return files.filter((f) => f.kind === kind);
  };

  return (
    <Card
      size="small"
      title={
        <Space>
          <span>📚 chunks/ 切分产物 （{data.length}）</span>
          {selectedStem && (
            <Tag color="processing">已选中：{selectedStem}</Tag>
          )}
        </Space>
      }
      extra={
        <Space>
          <Tooltip title="点击行首的「查看 chunks」可加载该文档的 chunk 列表与预览">
            <EyeOutlined style={{ color: "#999" }} />
          </Tooltip>
          <Button size="small" icon={<ReloadOutlined />} onClick={load}>
            刷新
          </Button>
        </Space>
      }
    >
      <Table
        size="small"
        rowKey="stem"
        loading={loading}
        dataSource={data}
        pagination={{ pageSize: 10, showSizeChanger: false }}
        rowClassName={(record) =>
          record.stem === selectedStem ? "ant-table-row-selected" : ""
        }
        expandable={{
          expandedRowRender: (record) => {
            const files = expanded[record.stem];
            const isLoading = expandedLoading[record.stem];
            const visible = filteredFiles(record.stem);
            const activeKind = fileKindFilter[record.stem] ?? "all";
            if (isLoading) return <span style={{ color: "#999" }}>加载中…</span>;
            if (!files) return null;
            return (
              <div>
                <Space style={{ marginBottom: 8 }}>
                  <Radio.Group
                    size="small"
                    value={activeKind}
                    onChange={(e) =>
                      setFileKindFilter((m) => ({ ...m, [record.stem]: e.target.value }))
                    }
                  >
                    <Radio.Button value="all">全部 ({files.length})</Radio.Button>
                    <Radio.Button value="chunk">
                      chunk ({files.filter((f) => f.kind === "chunk").length})
                    </Radio.Button>
                    <Radio.Button value="image">
                      image ({files.filter((f) => f.kind === "image").length})
                    </Radio.Button>
                    <Radio.Button value="metadata">
                      metadata ({files.filter((f) => f.kind === "metadata").length})
                    </Radio.Button>
                  </Radio.Group>
                </Space>
                <Table
                  size="small"
                  rowKey="rel_path"
                  dataSource={visible}
                  pagination={{ pageSize: 10, showSizeChanger: false }}
                  columns={[
                    {
                      title: "类型",
                      dataIndex: "kind",
                      width: 90,
                      render: (v: string) => (
                        <Tag color={KIND_COLORS[v] ?? "default"}>
                          <Space size={4}>
                            {KIND_ICONS[v] ?? null}
                            <span>{v}</span>
                          </Space>
                        </Tag>
                      ),
                    },
                    {
                      title: "路径",
                      dataIndex: "rel_path",
                      ellipsis: true,
                      render: (v: string, file: ChunkFile) =>
                        file.kind === "image" ? (
                          <Tooltip title={v}>
                            <code style={{ fontSize: 12 }}>{v}</code>
                          </Tooltip>
                        ) : (
                          v
                        ),
                    },
                    {
                      title: "扩展名",
                      dataIndex: "ext",
                      width: 100,
                    },
                    {
                      title: "大小",
                      dataIndex: "size",
                      width: 100,
                      render: (v: number) => formatBytes(v),
                    },
                  ]}
                />
              </div>
            );
          },
          onExpand: toggleExpand,
        }}
        columns={[
          {
            title: "文档",
            dataIndex: "stem",
            ellipsis: true,
            render: (v: string, record: ChunkSummary) => (
              <Space>
                <Button
                  size="small"
                  type={record.stem === selectedStem ? "primary" : "default"}
                  icon={<EyeOutlined />}
                  onClick={() => onSelect(record.stem === selectedStem ? null : record.stem)}
                >
                  {record.stem === selectedStem ? "取消选中" : "查看 chunks"}
                </Button>
                <Tooltip title={record.dir}>
                  <Space>
                    <FolderOpenOutlined />
                    <code>{v}</code>
                  </Space>
                </Tooltip>
              </Space>
            ),
          },
          {
            title: "chunk 数",
            dataIndex: "chunk_count",
            width: 100,
            render: (v: number) =>
              v > 0 ? (
                <Tag color="blue">{v}</Tag>
              ) : (
                <span style={{ color: "#bbb" }}>0</span>
              ),
          },
          {
            title: "图片数",
            dataIndex: "image_count",
            width: 90,
            render: (v: number) => v || <span style={{ color: "#bbb" }}>—</span>,
          },
          {
            title: "文件总数",
            dataIndex: "file_count",
            width: 100,
          },
          {
            title: "总大小",
            dataIndex: "total_size",
            width: 110,
            render: (v: number) => formatBytes(v),
          },
        ]}
      />
    </Card>
  );
}
