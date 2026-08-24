import {
  Button,
  Card,
  Select,
  Space,
  Statistic,
  Switch,
  Tooltip,
  Typography,
  message,
  Table,
  Tag,
} from "antd";
import {
  PlayCircleOutlined,
  ExperimentOutlined,
  ScissorOutlined,
  ReloadOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import { useEffect, useState } from "react";
import {
  triggerChunk,
  listChunkStrategies,
  type ChunkReport,
  type ChunkActionRecord,
  type ChunkStrategyOption,
} from "../api/client";

const { Text } = Typography;

interface Props {
  onAfterChunk: (r: ChunkReport) => void;
  lastReport: ChunkReport | null;
  loading: boolean;
  onLoadingChange: (v: boolean) => void;
}

const ACTION_COLORS: Record<string, string> = {
  chunked: "green",
  skipped_chunked: "blue",
  chunk_failed: "red",
  no_parsed: "orange",
  dry_run_chunk: "gold",
};

const ACTION_LABELS: Record<string, string> = {
  chunked: "已切分",
  skipped_chunked: "已切分跳过",
  chunk_failed: "失败",
  no_parsed: "无解析",
  dry_run_chunk: "试运行",
};

export default function ChunkControl({
  onAfterChunk,
  lastReport,
  loading,
  onLoadingChange,
}: Props) {
  const [dryRun, setDryRun] = useState(false);
  const [force, setForce] = useState(false);
  const [strategy, setStrategy] = useState<string>("");
  const [strategies, setStrategies] = useState<ChunkStrategyOption[]>([]);
  const [msgApi, contextHolder] = message.useMessage();

  // ★ 2026-08-24：加载可用切分策略（默认选中后端默认值）
  useEffect(() => {
    listChunkStrategies()
      .then((res) => {
        setStrategies(res.strategies ?? []);
        setStrategy((prev) => prev || res.default || "structure");
      })
      .catch(() => {
        /* 接口失败不阻塞页面，仅保持默认 */
      });
  }, []);

  const onRun = async () => {
    onLoadingChange(true);
    try {
      const report = await triggerChunk(dryRun, force, strategy);
      onAfterChunk(report);
      msgApi.success(
        `切分完成：扫描 ${report.scanned}，成功 ${report.chunked}，跳过 ${report.skipped_done}，失败 ${report.failed}`
      );
    } catch (e) {
      msgApi.error(`切分失败：${(e as Error).message}`);
    } finally {
      onLoadingChange(false);
    }
  };

  // 失败/no_parsed 行的明细（仅展示前 10 条）
  const problemActions: ChunkActionRecord[] =
    lastReport?.actions.filter(
      (a) => a.action === "chunk_failed" || a.action === "no_parsed"
    ) ?? [];

  return (
    <Card
      title={
        <Space>
          <ScissorOutlined />
          <span>执行 §3.3 切分</span>
        </Space>
      }
      extra={
        <Space wrap>
          <Tooltip
            title={
              strategy
                ? (strategies.find((s) => s.key === strategy)?.desc ?? "选择切分策略")
                : "选择切分策略"
            }
          >
            <Space>
              <Text style={{ fontSize: 12 }}>切分策略</Text>
              <Select
                size="small"
                style={{ minWidth: 150 }}
                value={strategy || undefined}
                placeholder="选择策略"
                onChange={setStrategy}
                options={strategies.map((s) => ({
                  value: s.key,
                  label: s.name,
                }))}
              />
            </Space>
          </Tooltip>
          <Tooltip title="开启后只识别不实际切分（不写盘、不移动文件、不动 manifest）">
            <Space>
              <ExperimentOutlined />
              <Text>试运行</Text>
              <Switch checked={dryRun} onChange={setDryRun} />
            </Space>
          </Tooltip>
          <Tooltip title="已切分过（chunks 列非空）的文档将清空重切；通常用于规则升级后批量重跑">
            <Space>
              <ReloadOutlined />
              <Text>强制重切</Text>
              <Switch
                checked={force}
                onChange={setForce}
                disabled={dryRun}
              />
            </Space>
          </Tooltip>
          <Button
            type="primary"
            icon={<PlayCircleOutlined />}
            loading={loading}
            onClick={onRun}
          >
            {dryRun
              ? "试运行切分"
              : force
                ? "强制重切"
                : "执行切分"}
          </Button>
        </Space>
      }
    >
      {contextHolder}
      <Space direction="vertical" size="small" style={{ width: "100%" }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          遍历 <code>manifest</code> 中 <code>parse</code> 列非空的行 →
          按所选切分策略（默认 <code>structure</code>，详见 <code>cutstrategy.md</code>）切分 →
          产物落 <code> data/chunks/&lt;stem&gt;/</code>，并写 <code>chunk_metadata.json</code> + 拷贝图片到
          <code> chunks/&lt;stem&gt;/images/</code>。成功后 <code>manifest.chunks</code> 列写入 stem，<code>status</code> = <code>chunked</code>。
          <br />
          语义切分 / 晚切分需配置 Embedding（Dify 或 <code>chunk_embedding_api_url</code>），LLM 切分默认关闭，未就绪时自动降级。
        </Text>
        <Space size="large" wrap>
          <Statistic title="本次扫描文件数" value={lastReport?.scanned ?? 0} />
          <Statistic
            title="已切分"
            value={lastReport?.chunked ?? 0}
            valueStyle={{
              color: lastReport && lastReport.chunked > 0 ? "#52c41a" : undefined,
            }}
          />
          <Statistic title="已切分跳过" value={lastReport?.skipped_done ?? 0} />
          <Statistic
            title="失败"
            value={lastReport?.failed ?? 0}
            valueStyle={{
              color: lastReport && lastReport.failed > 0 ? "#cf1322" : undefined,
            }}
          />
        </Space>

        {problemActions.length > 0 && (
          <div>
            <Space style={{ marginBottom: 8 }}>
              <WarningOutlined style={{ color: "#faad14" }} />
              <Text strong style={{ fontSize: 13 }}>
                本次失败 / 跳过明细（{problemActions.length}）
              </Text>
            </Space>
            <Table
              size="small"
              rowKey="filename"
              dataSource={problemActions}
              pagination={{ pageSize: 5, showSizeChanger: false }}
              columns={[
                {
                  title: "文件",
                  dataIndex: "filename",
                  ellipsis: true,
                  width: 240,
                },
                {
                  title: "动作",
                  dataIndex: "action",
                  width: 130,
                  render: (v: string) => (
                    <Tag color={ACTION_COLORS[v] ?? "default"}>
                      {ACTION_LABELS[v] ?? v}
                    </Tag>
                  ),
                },
                {
                  title: "说明",
                  dataIndex: "error",
                  ellipsis: true,
                  render: (v: string | null | undefined) =>
                    v ? (
                      <span style={{ color: "#cf1322", fontSize: 12 }}>{v}</span>
                    ) : (
                      <span style={{ color: "#bbb" }}>—</span>
                    ),
                },
              ]}
            />
          </div>
        )}
      </Space>
    </Card>
  );
}
