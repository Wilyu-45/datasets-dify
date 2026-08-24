import { Space, Typography } from "antd";
import { useState } from "react";
import PipelineControl from "../components/PipelineControl";
import BatchFileUpload from "../components/BatchFileUpload";
import ManifestTable from "../components/ManifestTable";
import {
  type BatchUploadResponse,
  type ChunkReport,
  type DifyUploadReport,
  type ParseReport,
  type PipelineReport,
} from "../api/client";
import DifyReportTable from "../components/DifyReportTable";

const { Title, Paragraph } = Typography;

export default function PipelinePage() {
  const [refreshKey, setRefreshKey] = useState(0);
  const [loading, setLoading] = useState(false);
  const [lastReport, setLastReport] = useState<PipelineReport | null>(null);

  const onAfterPipeline = (r: PipelineReport) => {
    setLastReport(r);
    setRefreshKey((k) => k + 1);
  };

  // 批量上传后刷新 manifest；如果开启了 auto_ingest，整批 pipeline 报告也会回填
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
          步骤 3.0 · 一键流水线
        </Title>
        <Paragraph type="secondary" style={{ marginTop: 4, marginBottom: 0 }}>
          按顺序串联执行 <code>① 扫描</code> → <code>② 解析</code> →{" "}
          <code>③ 切分</code> → <code>④ 入库</code> 4 个阶段。 单阶段失败
          不会中断后续阶段（除非显式 stop_on_error），下方按阶段展开明细报告。
        </Paragraph>
      </div>

      {/* 批量文件上传（2026-08 新版，替代 SingleFileUpload） */}
      <BatchFileUpload
        onAfterUpload={onAfterBatchUpload}
        onLoadingChange={setLoading}
      />

      <PipelineControl
        onAfterPipeline={onAfterPipeline}
        lastReport={lastReport}
        loading={loading}
        onLoadingChange={setLoading}
      />

      {lastReport?.dify && (
        <Space direction="vertical" size="small" style={{ width: "100%" }}>
          <Title level={5} style={{ margin: 0 }}>
            ④ 入库阶段明细
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
