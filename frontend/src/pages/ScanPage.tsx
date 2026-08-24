import { Col, Row, Space, Typography } from "antd";
import { useState } from "react";
import ScanControl from "../components/ScanControl";
import FileTable from "../components/FileTable";
import ManifestTable from "../components/ManifestTable";
import type { ScanReport } from "../api/client";

const { Title, Paragraph } = Typography;

export default function ScanPage() {
  const [refreshKey, setRefreshKey] = useState(0);
  const [loading, setLoading] = useState(false);
  const [lastReport, setLastReport] = useState<ScanReport | null>(null);

  const onAfterScan = (r: ScanReport) => {
    setLastReport(r);
    setRefreshKey((k) => k + 1);
  };

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <div>
        <Title level={4} style={{ margin: 0 }}>
          步骤 3.1 · 文件读取与状态管理
        </Title>
        <Paragraph type="secondary" style={{ marginTop: 4, marginBottom: 0 }}>
          扫描 <code>data/input/</code> → 与 <code>manifest.xlsx</code> 比对 → 把未处理（status ≠ "done"）的文件移动到 <code>data/pending/</code>。
        </Paragraph>
      </div>

      <ScanControl
        onAfterScan={onAfterScan}
        lastReport={lastReport}
        loading={loading}
        onLoadingChange={setLoading}
      />

      <Row gutter={16}>
        <Col xs={24} lg={12}>
          <FileTable dir="input" refreshKey={refreshKey} />
        </Col>
        <Col xs={24} lg={12}>
          <FileTable dir="pending" refreshKey={refreshKey} />
        </Col>
      </Row>

      <ManifestTable refreshKey={refreshKey} />
    </Space>
  );
}
