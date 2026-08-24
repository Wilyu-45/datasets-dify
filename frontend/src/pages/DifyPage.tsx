import { Space, Typography } from "antd";
import { useState } from "react";
import DifyControl from "../components/DifyControl";
import DifyReportTable from "../components/DifyReportTable";
import ManifestTable from "../components/ManifestTable";
import type { DifyUploadReport } from "../api/client";

const { Title, Paragraph } = Typography;

export default function DifyPage() {
  const [refreshKey, setRefreshKey] = useState(0);
  const [loading, setLoading] = useState(false);
  const [lastReport, setLastReport] = useState<DifyUploadReport | null>(null);

  const onAfterUpload = (r: DifyUploadReport) => {
    setLastReport(r);
    setRefreshKey((k) => k + 1);
  };

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <div>
        <Title level={4} style={{ margin: 0 }}>
          步骤 3.4 · Dify 入库
        </Title>
        <Paragraph type="secondary" style={{ marginTop: 4, marginBottom: 0 }}>
          遍历 <code>data/chunks/</code> 下所有以文档命名的子目录，对每个目录在
          Dify 知识库（dataset_id 来自配置）创建一个独立文档（文档名 = stem，
          方便溯源），等待其 <code>indexing_status=completed</code> →
          逐 chunk 上传 chunk 内 <code>![](images/xxx)</code> 引用的图片到 Dify
          → 调 <code>add_segments</code> 把 chunk 内容 + 关联图片
          <code>attachment_ids</code> 一并写入。成功后
          <code> manifest.dify_doc_id</code> 写入 Dify 文档 ID，
          <code>dify_status</code> = <code>done</code>，
          <strong style={{ color: "#d48806" }}>并把目录从 <code>data/chunks/</code> 归档到 <code>data/output/</code></strong>，
          <code>manifest.chunks</code> 同步更新为 <code>output/&lt;stem&gt;</code>，
          <code>status</code> = <code>done</code>。
        </Paragraph>
      </div>

      <DifyControl
        onAfterUpload={onAfterUpload}
        lastReport={lastReport}
        loading={loading}
        onLoadingChange={setLoading}
      />

      <DifyReportTable lastReport={lastReport} />

      <ManifestTable refreshKey={refreshKey} difyReport={lastReport} />
    </Space>
  );
}
