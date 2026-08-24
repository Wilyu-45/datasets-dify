import { Card, Table, Tag, Button, Space, Tooltip } from "antd";
import { ReloadOutlined, FolderOpenOutlined } from "@ant-design/icons";
import { useEffect, useState } from "react";
import { listParsed, listParsedFiles, type ParsedDirItem, type ParsedFileItem } from "../api/client";

function formatBytes(b: number): string {
  if (b < 1024) return `${b} B`;
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
  return `${(b / 1024 / 1024).toFixed(2)} MB`;
}

export default function ParsedTable({ refreshKey }: { refreshKey: number }) {
  const [data, setData] = useState<ParsedDirItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState<Record<string, ParsedFileItem[]>>({});
  const [expandedLoading, setExpandedLoading] = useState<Record<string, boolean>>({});

  const load = async () => {
    setLoading(true);
    try {
      setData(await listParsed());
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey]);

  const toggleExpand = async (isOpen: boolean, record: ParsedDirItem) => {
    if (!isOpen) return;
    if (expanded[record.stem]) return;
    setExpandedLoading((m) => ({ ...m, [record.stem]: true }));
    try {
      const files = await listParsedFiles(record.stem);
      setExpanded((m) => ({ ...m, [record.stem]: files }));
    } finally {
      setExpandedLoading((m) => ({ ...m, [record.stem]: false }));
    }
  };

  return (
    <Card
      size="small"
      title={`📚 parsed/ 解析产物 （${data.length}）`}
      extra={
        <Button size="small" icon={<ReloadOutlined />} onClick={load}>
          刷新
        </Button>
      }
    >
      <Table
        size="small"
        rowKey="stem"
        loading={loading}
        dataSource={data}
        pagination={{ pageSize: 10, showSizeChanger: false }}
        expandable={{
          expandedRowRender: (record) => {
            const files = expanded[record.stem];
            const isLoading = expandedLoading[record.stem];
            if (isLoading) return <span style={{ color: "#999" }}>加载中…</span>;
            if (!files) return null;
            return (
              <Table
                size="small"
                rowKey="rel_path"
                dataSource={files}
                pagination={false}
                columns={[
                  { title: "路径", dataIndex: "rel_path", ellipsis: true },
                  { title: "扩展名", dataIndex: "ext", width: 100 },
                  {
                    title: "大小",
                    dataIndex: "size",
                    width: 100,
                    render: (v: number) => formatBytes(v),
                  },
                ]}
              />
            );
          },
          onExpand: toggleExpand,
        }}
        columns={[
          {
            title: "文档",
            dataIndex: "stem",
            ellipsis: true,
            render: (v: string, record: ParsedDirItem) => (
              <Tooltip title={record.dir}>
                <Space>
                  <FolderOpenOutlined />
                  <code>{v}</code>
                </Space>
              </Tooltip>
            ),
          },
          {
            title: ".md",
            dataIndex: "md",
            width: 80,
            render: (v: string | null) =>
              v ? <Tag color="green">✓</Tag> : <Tag>—</Tag>,
          },
          {
            title: ".json",
            dataIndex: "json",
            width: 80,
            render: (v: string | null) =>
              v ? <Tag color="green">✓</Tag> : <Tag>—</Tag>,
          },
          {
            title: "图片数",
            dataIndex: "image_count",
            width: 80,
            render: (v: number) => v || <span style={{ color: "#bbb" }}>—</span>,
          },
          {
            title: "文件数",
            dataIndex: "file_count",
            width: 80,
          },
          {
            title: "总大小",
            dataIndex: "total_size",
            width: 100,
            render: (v: number) => formatBytes(v),
          },
        ]}
      />
    </Card>
  );
}
