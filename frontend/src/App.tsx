import { Layout, Menu, Typography } from "antd";
import {
  FileTextOutlined,
  ScissorOutlined,
  CheckCircleOutlined,
  ThunderboltOutlined,
  SettingOutlined,
} from "@ant-design/icons";
import { useState } from "react";
import ParsePage from "./pages/ParsePage";
import ChunkPage from "./pages/ChunkPage";
import PipelinePage from "./pages/PipelinePage";
import VerifyPage from "./pages/VerifyPage";
import ConfigPage from "./pages/ConfigPage";

const { Header, Content, Sider } = Layout;
const { Title } = Typography;

export default function App() {
  // 默认进入「入库工作台」—— 上传即处理，用户的最高频入口
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
          文档入库系统
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
                label: "入库工作台",
              },
              {
                key: "parse",
                icon: <FileTextOutlined />,
                label: "解析产物",
              },
              {
                key: "chunk",
                icon: <ScissorOutlined />,
                label: "切分产物",
              },
              {
                key: "verify",
                icon: <CheckCircleOutlined />,
                label: "人工校验",
              },
              {
                key: "config",
                icon: <SettingOutlined />,
                label: "配置中心",
              },
            ]}
          />
        </Sider>
        <Content style={{ padding: 24, overflow: "auto" }}>
          {page === "pipeline" && (
            <PipelinePage onOpenConfig={() => setPage("config")} />
          )}
          {page === "parse" && <ParsePage />}
          {page === "chunk" && <ChunkPage />}
          {page === "verify" && <VerifyPage />}
          {page === "config" && <ConfigPage />}
        </Content>
      </Layout>
    </Layout>
  );
}
