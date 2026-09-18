import { Button, Card, Col, Dropdown, message, Row, Space, Typography } from "antd";
import { useState } from "react";
import ChunksTable from "../components/ChunksTable";
import ChunkDetail from "../components/ChunkDetail";
import ActiveConfigCard from "../components/ActiveConfigCard";
import { triggerChunk } from "../api/client";

const { Title, Paragraph } = Typography;

export default function ChunkPage() {
  const [selectedStem, setSelectedStem] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [loading, setLoading] = useState(false);

  // ★ 2026-09 生产部署改造：本页可独立触发切分（使用当前激活的切分策略）
  const run = async (force: boolean) => {
    setLoading(true);
    try {
      const r = await triggerChunk(false, force);
      message.success(
        `切分完成：成功 ${r.chunked}，跳过 ${r.skipped_done}，失败 ${r.failed}`
      );
      setRefreshKey((k) => k + 1);
    } catch (e) {
      message.error(`切分失败：${(e as Error).message}`);
    } finally {
      setLoading(false);
    }
  };

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

      <Card size="small" title="分环节处理">
        <Space wrap align="center">
          <Dropdown
            trigger={["click"]}
            menu={{
              items: [
                { key: "normal", label: "正常执行（跳过已切分）" },
                { key: "force", label: "强制执行（重新切分全部）" },
              ],
              onClick: ({ key }) => run(key === "force"),
            }}
          >
            <Button loading={loading}>重新切分</Button>
          </Dropdown>
          <Paragraph type="secondary" style={{ margin: 0 }}>
            按当前激活配置的切分策略重新切分，产物写入 <code>data/chunks/</code>。
          </Paragraph>
        </Space>
      </Card>

      <ActiveConfigCard />

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
    </Space>
  );
}
