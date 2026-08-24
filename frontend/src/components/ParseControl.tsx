import {
  Button,
  Card,
  Space,
  Statistic,
  Switch,
  Tooltip,
  Typography,
  message,
} from "antd";
import {
  PlayCircleOutlined,
  ExperimentOutlined,
  ApiOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import { useState } from "react";
import { triggerParse, type ParseReport } from "../api/client";

const { Text } = Typography;

interface Props {
  onAfterParse: (r: ParseReport) => void;
  lastReport: ParseReport | null;
  loading: boolean;
  onLoadingChange: (v: boolean) => void;
}

export default function ParseControl({
  onAfterParse,
  lastReport,
  loading,
  onLoadingChange,
}: Props) {
  const [dryRun, setDryRun] = useState(false);
  // ★ 2026-08 修复（流水线一致性）：手动解析页也支持 force 开关
  const [force, setForce] = useState(false);
  const [msgApi, contextHolder] = message.useMessage();

  const onRun = async () => {
    onLoadingChange(true);
    try {
      const report = await triggerParse(dryRun, force);
      onAfterParse(report);
      msgApi.success(
        `解析完成：扫描 ${report.scanned}，成功 ${report.parsed}，跳过 ${report.skipped_done}，失败 ${report.failed}`
      );
    } catch (e) {
      msgApi.error(`解析失败：${(e as Error).message}`);
    } finally {
      onLoadingChange(false);
    }
  };

  return (
    <Card
      title={
        <Space>
          <ApiOutlined />
          <span>执行 §3.2 MinerU 解析</span>
        </Space>
      }
      extra={
        <Space>
          <Tooltip title="开启后只识别不调 API（不写盘、不移动文件）">
            <Space>
              <ExperimentOutlined />
              <Text>试运行</Text>
              <Switch checked={dryRun} onChange={setDryRun} />
            </Space>
          </Tooltip>
          <Tooltip title="已解析过（parse 列非空）的文档将清空旧 parsed 目录并重新调 MinerU；通常用于 MinerU 升级 / PyMuPDF fallback 规则变更后批量重跑">
            <Space>
              <ReloadOutlined />
              <Text>强制重解析</Text>
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
              ? "试运行解析"
              : force
                ? "强制重解析"
                : "执行解析"}
          </Button>
        </Space>
      }
    >
      {contextHolder}
      <Space direction="vertical" size="small" style={{ width: "100%" }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          MinerU API:&nbsp;
          <code>{lastReport?.api_url ?? "(未执行)"}</code>
        </Text>
        <Space size="large" wrap>
          <Statistic title="本次扫描文件数" value={lastReport?.scanned ?? 0} />
          <Statistic
            title="已解析"
            value={lastReport?.parsed ?? 0}
            valueStyle={{ color: lastReport && lastReport.parsed > 0 ? "#52c41a" : undefined }}
          />
          <Statistic title="已解析跳过" value={lastReport?.skipped_done ?? 0} />
          <Statistic
            title="失败"
            value={lastReport?.failed ?? 0}
            valueStyle={{ color: lastReport && lastReport.failed > 0 ? "#cf1322" : undefined }}
          />
        </Space>
      </Space>
    </Card>
  );
}
