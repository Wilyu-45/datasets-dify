import { Space, Typography } from "antd";
import ParsedTable from "../components/ParsedTable";
import ActiveConfigCard from "../components/ActiveConfigCard";

const { Title, Paragraph } = Typography;

export default function ParsePage() {
  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <div>
        <Title level={4} style={{ margin: 0 }}>
          解析产物
        </Title>
        <Paragraph type="secondary" style={{ marginTop: 4, marginBottom: 0 }}>
          上传入库时自动调用 MinerU 解析（支持 PDF / Word / PPT / Excel 等），
          产物写入 <code>data/parsed/</code>。此处可查看所有已解析产物。
        </Paragraph>
      </div>

      <ActiveConfigCard />

      <ParsedTable refreshKey={0} />
    </Space>
  );
}
