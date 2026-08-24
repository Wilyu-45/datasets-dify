import { Col, Row, Space, Typography } from "antd";
import { useState } from "react";
import ChunkControl from "../components/ChunkControl";
import ChunksTable from "../components/ChunksTable";
import ChunkDetail from "../components/ChunkDetail";
import ManifestTable from "../components/ManifestTable";
import type { ChunkReport } from "../api/client";

const { Title, Paragraph } = Typography;

export default function ChunkPage() {
  const [refreshKey, setRefreshKey] = useState(0);
  const [loading, setLoading] = useState(false);
  const [lastReport, setLastReport] = useState<ChunkReport | null>(null);
  const [selectedStem, setSelectedStem] = useState<string | null>(null);

  const onAfterChunk = (r: ChunkReport) => {
    setLastReport(r);
    setRefreshKey((k) => k + 1);
  };

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <div>
        <Title level={4} style={{ margin: 0 }}>
          步骤 3.3 · 自定义切分
        </Title>
        <Paragraph type="secondary" style={{ marginTop: 4, marginBottom: 0 }}>
          对 <code>manifest</code> 中 <code>parse</code> 列非空的行调
          <code> chunker.chunk_parsed</code> 按 <code>cutrule.md</code> +
          <code> cutstrategy.md</code> 切分 →
          产物落 <code>data/chunks/&lt;stem&gt;/</code>
          （<code>chunk_NNN_xxx.md</code> + <code>chunk_metadata.json</code> +
          引用图片拷贝到 <code>images/</code>）。manifest 的
          <code> chunks</code> 列会同步更新为 stem，<code>status</code> →
          <code> chunked</code>。失败行原文件已在 §3.2 处理过，此处只更新
          <code> chunks</code> 列与 <code>error_msg</code>。
        </Paragraph>
      </div>

      <ChunkControl
        onAfterChunk={onAfterChunk}
        lastReport={lastReport}
        loading={loading}
        onLoadingChange={setLoading}
      />

      <Row gutter={16}>
        <Col xs={24} lg={24}>
          <ChunksTable
            refreshKey={refreshKey}
            selectedStem={selectedStem}
            onSelect={setSelectedStem}
          />
        </Col>
      </Row>

      <Row gutter={16}>
        <Col xs={24} lg={24}>
          <ChunkDetail stem={selectedStem} refreshKey={refreshKey} />
        </Col>
      </Row>

      <ManifestTable refreshKey={refreshKey} chunkReport={lastReport} />
    </Space>
  );
}
