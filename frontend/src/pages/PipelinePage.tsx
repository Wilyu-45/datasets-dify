import { Alert, Button, Space, Typography } from "antd";
import { useEffect, useState } from "react";
import BatchFileUpload from "../components/BatchFileUpload";
import ManifestTable from "../components/ManifestTable";
import {
  getActiveConfig,
  type BatchUploadResponse,
  type ChunkReport,
  type DifyUploadReport,
  type ParseReport,
  type PipelineReport,
} from "../api/client";
import DifyReportTable from "../components/DifyReportTable";

const { Title, Paragraph } = Typography;

interface Props {
  /** ★ 2026-08：跳转配置中心 */
  onOpenConfig?: () => void;
}

export default function PipelinePage({ onOpenConfig }: Props) {
  const [refreshKey, setRefreshKey] = useState(0);
  const [lastReport, setLastReport] = useState<PipelineReport | null>(null);
  // ★ 2026-08-31：当前激活配置方案（仅文档处理配置可用于上传入库；
  // 两套配置各自独立激活，上传页取 upload 类型的激活方案）
  const [activeProfileId, setActiveProfileId] = useState<string | undefined>();
  const [activeProfileName, setActiveProfileName] = useState<string | undefined>();
  // 未激活文档处理配置时给出提示
  const [activeIsWebscrape, setActiveIsWebscrape] = useState(false);

  // 加载当前激活配置方案（上传处理将使用它；上传页只用文档处理配置）
  useEffect(() => {
    getActiveConfig()
      .then((r) => {
        const isWeb = (r.profile?.type ?? "upload") === "webscrape";
        setActiveIsWebscrape(isWeb || !r.profile);
        if (isWeb || !r.profile) {
          setActiveProfileId(undefined);
          setActiveProfileName(undefined);
        } else {
          setActiveProfileId(r.profile?.id);
          setActiveProfileName(r.profile?.name);
        }
      })
      .catch(() => {
        setActiveProfileId(undefined);
        setActiveProfileName(undefined);
      });
  }, []);

  // 批量上传后刷新 manifest；上传即自动入库，整批 pipeline 报告也会回填
  const onAfterBatchUpload = (r: BatchUploadResponse) => {
    setRefreshKey((k) => k + 1);
    if (r.pipeline) {
      setLastReport(r.pipeline);
    }
  };

  // 把 PipelineReport 中各阶段子 report 适配为子组件期望的 prop
  const parseReport: ParseReport | null = lastReport?.parse
    ? (lastReport.parse as ParseReport)
    : null;
  const chunkReport: ChunkReport | null = lastReport?.chunk
    ? (lastReport.chunk as ChunkReport)
    : null;
  const difyReport: DifyUploadReport | null = lastReport?.dify
    ? (lastReport.dify as DifyUploadReport)
    : null;

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <div>
        <Title level={4} style={{ margin: 0 }}>
          入库工作台
        </Title>
        <Paragraph type="secondary" style={{ marginTop: 4, marginBottom: 0 }}>
          上传文档后自动登记并全流程处理：
          <code>① 解析</code> → <code>② 切分</code> → <code>③ 入库</code>。
          处理前请先在<strong>配置中心</strong>配置<strong>文档处理配置</strong>
          （知识库 ID 与切分策略）并激活；网站抓取配置不用于本页。
          下方可查看入库明细与文件清单。
        </Paragraph>
      </div>
      {activeIsWebscrape && (
        <Alert
          type="warning"
          showIcon
          message="尚未激活「文档处理配置」，上传入库不可用"
          description="请先到配置中心创建并激活文档处理配置（与网站抓取配置各自独立激活，互不影响）。当前激活的网站抓取配置仅用于网站抓取页。"
          action={
            <Button type="link" size="small" onClick={onOpenConfig}>
              去配置中心激活文档处理配置
            </Button>
          }
        />
      )}
      <BatchFileUpload
        onAfterUpload={onAfterBatchUpload}
        profileId={activeProfileId}
        profileName={activeProfileName}
        onOpenConfig={onOpenConfig}
      />

      {lastReport?.dify && (
        <Space direction="vertical" size="small" style={{ width: "100%" }}>
          <Title level={5} style={{ margin: 0 }}>
            入库阶段明细
          </Title>
          <DifyReportTable lastReport={difyReport} />
        </Space>
      )}

      <ManifestTable
        refreshKey={refreshKey}
        parseReport={parseReport}
        chunkReport={chunkReport}
        difyReport={difyReport}
      />
    </Space>
  );
}
