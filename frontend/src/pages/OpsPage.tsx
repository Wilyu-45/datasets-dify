/**
 * 运维页（2026-09 生产部署改造 C5）。
 *
 * 提供「立即清理」手动入口，调用 `POST /api/cleanup` 并展示清理报告；
 * 同时展示最近一次清理结果（定时任务与手动触发共享同一份 last_run）。
 *
 * 清理策略（后端硬编码 + 配置项控制保留期）：
 * - 永久保留：data/input（源文件）、data/parsed（解析产物）
 * - 可清理：chunks / output / error / webscrape（按保留期）
 * - 越界路径一律拒绝清理（防配置误填）
 */
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Descriptions,
  message,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { ReloadOutlined } from "@ant-design/icons";
import { useCallback, useEffect, useState } from "react";
import {
  getCleanupStatus,
  triggerCleanup,
  type CleanupReport,
  type DirStat,
} from "../api/client";

const { Title, Paragraph, Text } = Typography;

function formatBytes(b: number): string {
  if (b < 1024) return `${b} B`;
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
  return `${(b / 1024 / 1024).toFixed(2)} MB`;
}

function formatTime(v?: string | null): string {
  if (!v) return "-";
  return v.replace("T", " ").slice(0, 19);
}

const DIR_COLUMNS: ColumnsType<DirStat> = [
  {
    title: "目标目录",
    dataIndex: "dir",
    key: "dir",
    render: (v: string) => <Text code>{v}</Text>,
  },
  {
    title: "保留期",
    dataIndex: "retention_days",
    key: "retention_days",
    width: 100,
    render: (v: number) => `${v} 天`,
  },
  {
    title: "超期项",
    dataIndex: "expired_entries",
    key: "expired_entries",
    width: 90,
  },
  {
    title: "删除文件",
    dataIndex: "removed_files",
    key: "removed_files",
    width: 90,
  },
  {
    title: "删除目录",
    dataIndex: "removed_dirs",
    key: "removed_dirs",
    width: 90,
  },
  {
    title: "释放空间",
    dataIndex: "freed_bytes",
    key: "freed_bytes",
    width: 110,
    render: (v: number) => formatBytes(v),
  },
  {
    title: "错误",
    dataIndex: "errors",
    key: "errors",
    render: (errs: string[]) =>
      errs && errs.length > 0 ? (
        <Text type="danger" style={{ fontSize: 12 }}>
          {errs.join("；")}
        </Text>
      ) : (
        <Tag color="green">无</Tag>
      ),
  },
];

export default function OpsPage() {
  const [report, setReport] = useState<CleanupReport | null>(null);
  const [dryRun, setDryRun] = useState(true);
  const [loading, setLoading] = useState(false);
  const [statusLoading, setStatusLoading] = useState(false);

  const loadStatus = useCallback(async () => {
    setStatusLoading(true);
    try {
      const r = await getCleanupStatus();
      setReport(r.last_run);
    } catch (e) {
      message.error(`读取清理状态失败：${(e as Error).message}`);
    } finally {
      setStatusLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  const runCleanup = async () => {
    setLoading(true);
    try {
      const r = await triggerCleanup(dryRun);
      setReport(r);
      if (dryRun) {
        message.success(
          `试运行完成：将删除 ${r.removed_files} 个文件 / ${r.removed_dirs} 个目录，预计释放 ${formatBytes(r.freed_bytes)}`
        );
      } else {
        message.success(
          `清理完成：删除 ${r.removed_files} 个文件 / ${r.removed_dirs} 个目录，释放 ${formatBytes(r.freed_bytes)}`
        );
      }
    } catch (e) {
      message.error(`清理失败：${(e as Error).message}`);
    } finally {
      setLoading(false);
    }
  };

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <div>
        <Title level={4} style={{ margin: 0 }}>
          运维
        </Title>
        <Paragraph type="secondary" style={{ marginTop: 4, marginBottom: 0 }}>
          为保障服务器存储空间，服务会对中间产物定期清理：
          <strong>永久保留</strong> <code>data/input/</code>（源文件）与{" "}
          <code>data/parsed/</code>（解析产物）；按保留期清理{" "}
          <code>chunks/</code>、<code>output/</code>、<code>error/</code>、
          <code>webscrape/</code>。此处可手动立即执行一次清理。
        </Paragraph>
      </div>

      <Alert
        type="info"
        showIcon
        message="清理安全性"
        description="清理仅作用于 data_root 内的目标目录，越界路径一律拒绝；源文件与解析产物永不自动删除。建议先「试运行」确认待删除内容后再实际执行。"
      />

      <Card size="small" title="立即清理">
        <Space wrap align="center">
          <Checkbox
            checked={dryRun}
            onChange={(e) => setDryRun(e.target.checked)}
          >
            试运行（只预览，不实际删除）
          </Checkbox>
          <Button
            type="primary"
            danger={!dryRun}
            loading={loading}
            onClick={runCleanup}
          >
            {dryRun ? "试运行清理" : "立即清理"}
          </Button>
          <Button
            icon={<ReloadOutlined />}
            loading={statusLoading}
            onClick={() => void loadStatus()}
          >
            刷新最近结果
          </Button>
        </Space>
      </Card>

      {report ? (
        <Card
          size="small"
          title={
            <Space>
              <span>清理报告</span>
              {report.dry_run ? (
                <Tag color="gold">试运行</Tag>
              ) : (
                <Tag color="green">实际执行</Tag>
              )}
            </Space>
          }
        >
          <Space direction="vertical" size="small" style={{ width: "100%" }}>
            <Descriptions size="small" column={{ xs: 1, md: 3 }}>
              <Descriptions.Item label="开始时间">
                {formatTime(report.started_at)}
              </Descriptions.Item>
              <Descriptions.Item label="结束时间">
                {formatTime(report.finished_at)}
              </Descriptions.Item>
              <Descriptions.Item label="耗时">
                {report.duration_ms} ms
              </Descriptions.Item>
              <Descriptions.Item label="删除文件数">
                {report.removed_files}
              </Descriptions.Item>
              <Descriptions.Item label="删除目录数">
                {report.removed_dirs}
              </Descriptions.Item>
              <Descriptions.Item label="释放空间">
                {formatBytes(report.freed_bytes)}
              </Descriptions.Item>
            </Descriptions>

            {report.errors && report.errors.length > 0 && (
              <Alert
                type="warning"
                showIcon
                message="清理过程中出现错误"
                description={report.errors.join("；")}
              />
            )}

            <Table<DirStat>
              size="small"
              rowKey={(r) => r.dir}
              columns={DIR_COLUMNS}
              dataSource={report.dirs}
              pagination={false}
            />
          </Space>
        </Card>
      ) : (
        <Card size="small" title="清理报告">
          <Text type="secondary">
            暂无清理记录（定时任务尚未执行过，也未手动触发过）。
          </Text>
        </Card>
      )}
    </Space>
  );
}