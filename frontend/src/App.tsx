import { Layout, Menu, Typography } from "antd";
import {
  ScanOutlined,
  FileTextOutlined,
  ScissorOutlined,
  CloudUploadOutlined,
  CheckCircleOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { useState } from "react";
import ScanPage from "./pages/ScanPage";
import ParsePage from "./pages/ParsePage";
import ChunkPage from "./pages/ChunkPage";
import DifyPage from "./pages/DifyPage";
import PipelinePage from "./pages/PipelinePage";
import VerifyPage from "./pages/VerifyPage";

const { Header, Content, Sider } = Layout;
const { Title } = Typography;

export default function App() {
  // 默认进入「3.0 一键流水线」—— 用户的最高频入口
  const [page, setPage] = useState<string>("pipeline");

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Header
        style={{
          background: "#001529",
          display: "flex",
          alignItems: "center",
          padding: "0 24px",
        }}
      >
        <Title level={4} style={{ color: "#fff", margin: 0 }}>
          RAG 批量入库 · v0.5
        </Title>
      </Header>
      <Layout>
        <Sider width={220} theme="light">
          <Menu
            mode="inline"
            selectedKeys={[page]}
            onClick={(e) => setPage(e.key)}
            style={{ height: "100%", borderRight: 0 }}
            items={[
              {
                key: "pipeline",
                icon: <ThunderboltOutlined />,
                label: "3.0 一键流水线",
              },
              {
                key: "scan",
                icon: <ScanOutlined />,
                label: "3.1 文件扫描",
              },
              {
                key: "parse",
                icon: <FileTextOutlined />,
                label: "3.2 MinerU 解析",
              },
              {
                key: "chunk",
                icon: <ScissorOutlined />,
                label: "3.3 切分",
              },
              {
                key: "dify",
                icon: <CloudUploadOutlined />,
                label: "3.4 Dify 入库",
              },
              {
                key: "verify",
                icon: <CheckCircleOutlined />,
                label: "3.5 人工校验",
              },
            ]}
          />
        </Sider>
        <Content style={{ padding: 24, overflow: "auto" }}>
          {page === "pipeline" && <PipelinePage />}
          {page === "scan" && <ScanPage />}
          {page === "parse" && <ParsePage />}
          {page === "chunk" && <ChunkPage />}
          {page === "dify" && <DifyPage />}
          {page === "verify" && <VerifyPage />}
        </Content>
      </Layout>
    </Layout>
  );
}
