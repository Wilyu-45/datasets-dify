import { Button, Card, Space, Statistic, Switch, Tooltip, Typography, message } from "antd";
import { PlayCircleOutlined, ExperimentOutlined, ReloadOutlined } from "@ant-design/icons";
import { useState } from "react";
import { triggerScan, type ScanReport } from "../api/client";

const { Text } = Typography;

interface Props {
  onAfterScan: (r: ScanReport) => void;
  lastReport: ScanReport | null;
  loading: boolean;
  onLoadingChange: (v: boolean) => void;
}

export default function ScanControl({
  onAfterScan,
  lastReport,
  loading,
  onLoadingChange,
}: Props) {
  const [dryRun, setDryRun] = useState(false);
  // ★ 2026-08 修复（流水线一致性）：手动扫描页也支持 force 开关
  const [force, setForce] = useState(false);
  const [msgApi, contextHolder] = message.useMessage();

  const onRun = async () => {
    onLoadingChange(true);
    try {
      const report = await triggerScan(dryRun, force);
      onAfterScan(report);
      msgApi.success(
        `扫描完成：${report.scanned} 个文件，处理 ${report.staged}，新增 ${report.new}，失败 ${report.failed}`
      );
    } catch (e) {
      msgApi.error(`扫描失败：${(e as Error).message}`);
    } finally {
      onLoadingChange(false);
    }
  };

  return (
    <Card
      title={
        <Space>
          <PlayCircleOutlined />
          <span>执行 §3.1 扫描</span>
        </Space>
      }
      extra={
        <Space>
          <Tooltip title="开启后只返回统计，不移动文件、不写 manifest">
            <Space>
              <ExperimentOutlined />
              <Text>试运行</Text>
              <Switch checked={dryRun} onChange={setDryRun} />
            </Space>
          </Tooltip>
          <Tooltip title="已 staged（import_status 非空）的行也重新扫描；通常用于 input/ 恢复 / pending/ 误删后批量重跑">
            <Space>
              <ReloadOutlined />
              <Text>强制重扫</Text>
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
              ? "试运行扫描"
              : force
                ? "强制重扫"
                : "执行扫描"}
          </Button>
        </Space>
      }
    >
      {contextHolder}
      <Space size="large" wrap>
        <Statistic
          title="本次扫描文件数"
          value={lastReport?.scanned ?? 0}
          prefix={<ReloadOutlined />}
        />
        <Statistic title="已移入 pending" value={lastReport?.staged ?? 0} />
        <Statistic title="新增" value={lastReport?.new ?? 0} />
        <Statistic title="已 done 跳过" value={lastReport?.skipped_done ?? 0} />
        <Statistic
          title="重命名"
          value={lastReport?.renamed ?? 0}
          valueStyle={{ color: lastReport && lastReport.renamed > 0 ? "#faad14" : undefined }}
        />
        <Statistic
          title="磁盘缺失"
          value={lastReport?.missing_on_disk ?? 0}
          valueStyle={{ color: lastReport && lastReport.missing_on_disk > 0 ? "#cf1322" : undefined }}
        />
        <Statistic
          title="失败"
          value={lastReport?.failed ?? 0}
          valueStyle={{ color: lastReport && lastReport.failed > 0 ? "#cf1322" : undefined }}
        />
      </Space>
    </Card>
  );
}
