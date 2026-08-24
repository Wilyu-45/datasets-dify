import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Progress,
  Row,
  Space,
  Statistic,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import {
  ThunderboltOutlined,
  ExperimentOutlined,
  ReloadOutlined,
  PlayCircleOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  WarningOutlined,
  ClockCircleOutlined,
} from "@ant-design/icons";
import { useState } from "react";
import {
  triggerPipeline,
  type PipelineReport,
  type PipelineStatus,
} from "../api/client";

const { Text, Paragraph } = Typography;

interface Props {
  onAfterPipeline: (r: PipelineReport) => void;
  lastReport: PipelineReport | null;
  loading: boolean;
  onLoadingChange: (v: boolean) => void;
}

const STATUS_COLORS: Record<PipelineStatus, string> = {
  ok: "green",
  partial: "gold",
  failed: "red",
  skipped: "default",
  pending: "blue",
};

const STATUS_LABELS: Record<PipelineStatus, string> = {
  ok: "全部成功",
  partial: "部分成功",
  failed: "全部失败",
  skipped: "全部跳过",
  pending: "执行中…",
};

const STATUS_ICONS: Record<PipelineStatus, React.ReactNode> = {
  ok: <CheckCircleOutlined style={{ color: "#52c41a" }} />,
  partial: <WarningOutlined style={{ color: "#faad14" }} />,
  failed: <CloseCircleOutlined style={{ color: "#cf1322" }} />,
  skipped: <ClockCircleOutlined style={{ color: "#999" }} />,
  pending: <ClockCircleOutlined style={{ color: "#1677ff" }} />,
};

export default function PipelineControl({
  onAfterPipeline,
  lastReport,
  loading,
  onLoadingChange,
}: Props) {
  const [dryRun, setDryRun] = useState(false);
  const [force, setForce] = useState(false);
  const [msgApi, contextHolder] = message.useMessage();

  const onRun = async () => {
    onLoadingChange(true);
    try {
      const report = await triggerPipeline(dryRun, force);
      onAfterPipeline(report);
      const statusLabel = STATUS_LABELS[report.status] ?? report.status;
      if (report.status === "ok") {
        msgApi.success(
          `✅ 流水线完成：${statusLabel}，耗时 ${(report.duration_ms / 1000).toFixed(1)}s`
        );
      } else if (report.status === "partial") {
        msgApi.warning(
          `⚠️ 流水线部分成功：${report.error || ""}（${(report.duration_ms / 1000).toFixed(1)}s）`
        );
      } else if (report.status === "failed") {
        msgApi.error(
          `❌ 流水线失败：${report.error || "未知错误"}（${(report.duration_ms / 1000).toFixed(1)}s）`
        );
      } else if (report.status === "skipped") {
        msgApi.info("⏭ 全部阶段已跳过");
      }
    } catch (e) {
      msgApi.error(`流水线异常：${(e as Error).message}`);
    } finally {
      onLoadingChange(false);
    }
  };

  // 阶段统计
  const stepStats = [
    {
      key: "scan",
      title: "① 扫描",
      count: lastReport?.scan?.scanned ?? 0,
      success: lastReport?.scan?.staged ?? 0,
      failed: lastReport?.scan?.failed ?? 0,
      ms: lastReport?.step_timings_ms?.scan ?? 0,
    },
    {
      key: "parse",
      title: "② 解析",
      count: lastReport?.parse?.scanned ?? 0,
      success: lastReport?.parse?.parsed ?? 0,
      failed: lastReport?.parse?.failed ?? 0,
      ms: lastReport?.step_timings_ms?.parse ?? 0,
    },
    {
      key: "chunk",
      title: "③ 切分",
      count: lastReport?.chunk?.scanned ?? 0,
      success: lastReport?.chunk?.chunked ?? 0,
      failed: lastReport?.chunk?.failed ?? 0,
      ms: lastReport?.step_timings_ms?.chunk ?? 0,
    },
    {
      key: "dify",
      title: "④ 入库",
      count: lastReport?.dify?.scanned ?? 0,
      success: lastReport?.dify?.uploaded ?? 0,
      failed: lastReport?.dify?.failed ?? 0,
      ms: lastReport?.step_timings_ms?.dify ?? 0,
    },
  ];

  return (
    <Card
      title={
        <Space>
          <ThunderboltOutlined style={{ color: "#faad14" }} />
          <span>一键流水线（§3.0）</span>
        </Space>
      }
      extra={
        <Space>
          <Tooltip title="开启后只识别不实际写盘 / 不调外部 API / 不入库 Dify（pure 预演）">
            <Space>
              <ExperimentOutlined />
              <Text>试运行</Text>
              <Switch checked={dryRun} onChange={setDryRun} />
            </Space>
          </Tooltip>
          <Tooltip title="已切分 / 已入库 / 已解析 / 已扫描 的文档都会重做（force 全量重传）；通常用于 Dify 端数据丢失时全量回灌，或 MinerU 升级后重解析">
            <Space>
              <ReloadOutlined />
              <Text>强制重传</Text>
              <Switch
                checked={force}
                onChange={setForce}
                disabled={dryRun}
              />
            </Space>
          </Tooltip>
          <Button
            type="primary"
            danger={!dryRun}
            icon={<PlayCircleOutlined />}
            loading={loading}
            onClick={onRun}
            size="large"
          >
            {dryRun
              ? "试运行"
              : force
                ? "强制重传全流程"
                : "▶ 一键执行全流程"}
          </Button>
        </Space>
      }
    >
      {contextHolder}
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {/* 总体状态 */}
        <Card
          size="small"
          type="inner"
          title={
            <Space>
              {lastReport
                ? STATUS_ICONS[lastReport.status]
                : STATUS_ICONS.pending}
              <span>本次流水线总览</span>
            </Space>
          }
        >
          <Row gutter={16}>
            <Col xs={12} md={6}>
              <Statistic
                title="总状态"
                value={lastReport ? STATUS_LABELS[lastReport.status] : "未执行"}
                valueStyle={{
                  color: lastReport
                    ? STATUS_COLORS[lastReport.status] === "default"
                      ? undefined
                      : STATUS_COLORS[lastReport.status] === "red"
                        ? "#cf1322"
                        : STATUS_COLORS[lastReport.status] === "gold"
                          ? "#faad14"
                          : STATUS_COLORS[lastReport.status] === "green"
                            ? "#52c41a"
                            : undefined
                    : undefined,
                  fontSize: 16,
                }}
              />
            </Col>
            <Col xs={12} md={6}>
              <Statistic
                title="总耗时"
                value={lastReport ? (lastReport.duration_ms / 1000).toFixed(1) : 0}
                suffix="s"
                prefix={<ClockCircleOutlined />}
              />
            </Col>
            <Col xs={12} md={6}>
              <Statistic
                title="试运行模式"
                value={lastReport?.dry_run ? "是" : "否"}
                valueStyle={{ color: lastReport?.dry_run ? "#faad14" : undefined }}
              />
            </Col>
            <Col xs={12} md={6}>
              <Statistic
                title="强制重传"
                value={
                  lastReport?.chunk && lastReport.chunk.scanned > 0
                    ? "已启用"
                    : "未启用"
                }
              />
            </Col>
          </Row>

          {lastReport?.error && (
            <Alert
              type={lastReport.status === "partial" ? "warning" : "error"}
              showIcon
              style={{ marginTop: 12 }}
              message={
                <Space>
                  <Text>部分阶段错误：</Text>
                  <code style={{ fontSize: 12 }}>{lastReport.error}</code>
                </Space>
              }
            />
          )}
        </Card>

        {/* 阶段执行情况 */}
        <Row gutter={16}>
          {stepStats.map((s) => (
            <Col xs={12} md={6} key={s.key}>
              <Card size="small" type="inner">
                <Statistic
                  title={
                    <Space>
                      <span>{s.title}</span>
                      {s.ms > 0 && (
                        <Tag color="default" style={{ fontSize: 11 }}>
                          {(s.ms / 1000).toFixed(1)}s
                        </Tag>
                      )}
                    </Space>
                  }
                  value={s.count}
                  suffix={
                    <span style={{ fontSize: 12, color: "#999" }}>
                      扫描
                    </span>
                  }
                />
                <Space size="small" wrap style={{ marginTop: 4 }}>
                  <Tag color="green">成功 {s.success}</Tag>
                  {s.failed > 0 && <Tag color="red">失败 {s.failed}</Tag>}
                </Space>
                {s.count > 0 && (
                  <Progress
                    percent={Math.round((s.success / s.count) * 100)}
                    size="small"
                    status={s.failed > 0 ? "exception" : "success"}
                    showInfo={false}
                    style={{ marginTop: 4 }}
                  />
                )}
              </Card>
            </Col>
          ))}
        </Row>

        <Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 0 }}>
          点击「一键执行」后会按 <code>① 扫描</code> → <code>② 解析</code> →{" "}
          <code>③ 切分</code> → <code>④ 入库</code> 顺序串行执行 4 个阶段。
          单阶段失败不中断后续阶段（除非后端 stop_on_error 开启）。
          下方区域会按阶段展示 ScanReport / ParseReport / ChunkReport /
          DifyUploadReport 全量明细。
        </Paragraph>
      </Space>
    </Card>
  );
}
