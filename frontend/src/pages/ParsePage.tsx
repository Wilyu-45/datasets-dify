import { Col, Row, Space, Typography } from "antd";
import { useState } from "react";
import ParseControl from "../components/ParseControl";
import ParsedTable from "../components/ParsedTable";
import ManifestTable from "../components/ManifestTable";
import type { ParseReport } from "../api/client";

const { Title, Paragraph } = Typography;

export default function ParsePage() {
  const [refreshKey, setRefreshKey] = useState(0);
  const [loading, setLoading] = useState(false);
  const [lastReport, setLastReport] = useState<ParseReport | null>(null);

  const onAfterParse = (r: ParseReport) => {
    setLastReport(r);
    setRefreshKey((k) => k + 1);
  };

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <div>
        <Title level={4} style={{ margin: 0 }}>
          步骤 3.2 · MinerU 解析
        </Title>
        <Paragraph type="secondary" style={{ marginTop: 4, marginBottom: 0 }}>
          对 manifest 中「导入情况」已非空、且「parse」列为空的行，调 MinerU
          <code> /file_parse</code> 同步解析，结果落到 <code>data/parsed/&lt;stem&gt;/</code>，
          失败则原文件移入 <code>data/error/</code>。manifest 的 <code>parse</code> 列会同步更新。
        </Paragraph>
      </div>

      <ParseControl
        onAfterParse={onAfterParse}
        lastReport={lastReport}
        loading={loading}
        onLoadingChange={setLoading}
      />

      <Row gutter={16}>
        <Col xs={24} lg={24}>
          <ParsedTable refreshKey={refreshKey} />
        </Col>
      </Row>

      <ManifestTable refreshKey={refreshKey} lastReport={lastReport} />
    </Space>
  );
}
