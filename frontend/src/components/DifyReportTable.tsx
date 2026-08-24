import { Card, Space, Table, Tag, Tooltip, Typography } from "antd";
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  ExperimentOutlined,
  InfoCircleOutlined,
} from "@ant-design/icons";
import type { DifyActionRecord, DifyUploadReport } from "../api/client";

const { Text } = Typography;

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

const ACTION_ICONS: Record<string, React.ReactNode> = {
  uploaded: <CheckCircleOutlined />,
  skipped_done: <InfoCircleOutlined />,
  failed: <CloseCircleOutlined />,
  dry_run: <ExperimentOutlined />,
};

interface Props {
  lastReport: DifyUploadReport | null;
}

export default function DifyReportTable({ lastReport }: Props) {
  const actions = lastReport?.actions ?? [];

  if (!lastReport) {
    return (
      <Card
        size="small"
        title={
          <Space>
            <span>📊 本次 Dify 入库结果明细</span>
            <Tag>尚未执行</Tag>
          </Space>
        }
      >
        <Text type="secondary" style={{ fontSize: 12 }}>
          点击上方「执行入库」按钮后，这里会显示每个文档的入库动作、dify_doc_id、错误原因等明细。
        </Text>
      </Card>
    );
  }

  return (
    <Card
      size="small"
      title={
        <Space>
          <span>📊 本次 Dify 入库结果明细</span>
          <Tag color="blue">{actions.length} 条记录</Tag>
        </Space>
      }
    >
      <Table
        size="small"
        rowKey="stem"
        dataSource={actions}
        pagination={{ pageSize: 10, showSizeChanger: false }}
        columns={[
          {
            title: "文档 stem",
            dataIndex: "stem",
            ellipsis: true,
            width: 220,
            render: (v: string) => (
              <Tooltip title={v}>
                <code style={{ fontSize: 12 }}>{v}</code>
              </Tooltip>
            ),
          },
          {
            title: "动作",
            dataIndex: "action",
            width: 120,
            render: (v: string) => (
              <Tag color={ACTION_COLORS[v] ?? "default"}>
                <Space size={4}>
                  {ACTION_ICONS[v] ?? null}
                  <span>{ACTION_LABELS[v] ?? v}</span>
                </Space>
              </Tag>
            ),
          },
          {
            title: "Dify doc_id",
            dataIndex: "dify_doc_id",
            width: 260,
            ellipsis: true,
            render: (v: string | null | undefined) =>
              v ? (
                <Tooltip title={v}>
                  <code style={{ fontSize: 12 }}>{v}</code>
                </Tooltip>
              ) : (
                <span style={{ color: "#bbb" }}>—</span>
              ),
          },
          {
            title: "chunks_dir",
            dataIndex: "chunks_dir",
            ellipsis: true,
            render: (v: string | null | undefined) =>
              v ? (
                <Tooltip title={v}>
                  <code style={{ fontSize: 12 }}>{v}</code>
                </Tooltip>
              ) : (
                <span style={{ color: "#bbb" }}>—</span>
              ),
          },
          {
            title: "耗时 (ms)",
            dataIndex: "duration_ms",
            width: 110,
            render: (v: number | null | undefined) =>
              v != null ? (
                <span>{v.toLocaleString()}</span>
              ) : (
                <span style={{ color: "#bbb" }}>—</span>
              ),
          },
          {
            title: "说明 / 错误",
            dataIndex: "error",
            ellipsis: true,
            render: (_: unknown, record: DifyActionRecord) => {
              const text = record.error || record.note;
              if (!text) return <span style={{ color: "#bbb" }}>—</span>;
              const isError = record.action === "failed";
              return (
                <Tooltip title={text}>
                  <span
                    style={{
                      color: isError ? "#cf1322" : "#666",
                      fontSize: 12,
                    }}
                  >
                    {text}
                  </span>
                </Tooltip>
              );
            },
          },
        ]}
      />
    </Card>
  );
}
