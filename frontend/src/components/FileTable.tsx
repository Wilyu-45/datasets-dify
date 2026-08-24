import { Card, Table, Tag, Tooltip, Button, Space } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { useEffect, useState } from "react";
import { listFiles, type FileItem } from "../api/client";

interface Props {
  dir: "input" | "pending";
  refreshKey: number;
}

const STATUS_COLORS: Record<string, string> = {
  pending: "gold",
  done: "green",
  error: "red",
  new: "blue",
};

function formatBytes(b: number): string {
  if (b < 1024) return `${b} B`;
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
  return `${(b / 1024 / 1024).toFixed(2)} MB`;
}

export default function FileTable({ dir, refreshKey }: Props) {
  const [data, setData] = useState<FileItem[]>([]);
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      setData(await listFiles(dir));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dir, refreshKey]);

  return (
    <Card
      size="small"
      title={`${dir === "input" ? "📥 input/" : "📦 pending/"} （${data.length}）`}
      extra={
        <Button size="small" icon={<ReloadOutlined />} onClick={load}>
          刷新
        </Button>
      }
    >
      <Table
        size="small"
        rowKey="name"
        loading={loading}
        dataSource={data}
        pagination={{ pageSize: 10, showSizeChanger: false }}
        columns={[
          { title: "文件名", dataIndex: "name", ellipsis: true },
          {
            title: "大小",
            dataIndex: "size",
            width: 100,
            render: (v: number) => formatBytes(v),
          },
          {
            title: "mtime",
            dataIndex: "mtime",
            width: 160,
            render: (v: string) => new Date(v).toLocaleString(),
          },
          {
            title: "status",
            dataIndex: "status",
            width: 100,
            render: (v: string | null | undefined) =>
              v ? <Tag color={STATUS_COLORS[v] ?? "default"}>{v}</Tag> : <Tag>—</Tag>,
          },
          {
            title: "md5",
            dataIndex: "md5",
            width: 220,
            render: (v: string | null | undefined) =>
              v ? (
                <Tooltip title={v}>
                  <code style={{ fontSize: 12 }}>{v.slice(0, 12)}…</code>
                </Tooltip>
              ) : (
                <span style={{ color: "#bbb" }}>—</span>
              ),
          },
        ]}
      />
    </Card>
  );
}
