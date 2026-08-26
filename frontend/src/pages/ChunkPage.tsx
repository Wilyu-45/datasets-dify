import { Col, Row, Space, Typography } from "antd";
import { useState } from "react";
import ChunksTable from "../components/ChunksTable";
import ChunkDetail from "../components/ChunkDetail";
import ActiveConfigCard from "../components/ActiveConfigCard";

const { Title, Paragraph } = Typography;

export default function ChunkPage() {
  const [selectedStem, setSelectedStem] = useState<string | null>(null);

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <div>
        <Title level={4} style={{ margin: 0 }}>
          切分产物
        </Title>
        <Paragraph type="secondary" style={{ marginTop: 4, marginBottom: 0 }}>
          上传入库时自动按切分规则切分，产物写入 <code>data/chunks/</code>。
          此处可查看所有切分产物与片段详情。
        </Paragraph>
      </div>

      <ActiveConfigCard />

      <Row gutter={16}>
        <Col xs={24} lg={24}>
          <ChunksTable
            refreshKey={0}
            selectedStem={selectedStem}
            onSelect={setSelectedStem}
          />
        </Col>
      </Row>

      <Row gutter={16}>
        <Col xs={24} lg={24}>
          <ChunkDetail stem={selectedStem} refreshKey={0} />
        </Col>
      </Row>
    </Space>
  );
}
