import { Button, Card, Dropdown, message, Space, Typography } from "antd";
import { useState } from "react";
import ParsedTable from "../components/ParsedTable";
import { triggerParse } from "../api/client";

const { Title, Paragraph } = Typography;

export default function ParsePage() {
  const [refreshKey, setRefreshKey] = useState(0);
  const [loading, setLoading] = useState(false);

  // ★ 2026-09 生产部署改造：本页可独立触发解析（正常执行 / 强制执行）
  const run = async (force: boolean) => {
    setLoading(true);
    try {
      const r = await triggerParse(false, force);
      message.success(
        `解析完成：成功 ${r.parsed}，跳过 ${r.skipped_done}，失败 ${r.failed}`
      );
      setRefreshKey((k) => k + 1);
    } catch (e) {
      message.error(`解析失败：${(e as Error).message}`);
    } finally {
      setLoading(false);
    }
  };

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

      <Card size="small" title="分环节处理">
        <Space wrap align="center">
          <Dropdown
            trigger={["click"]}
            menu={{
              items: [
                { key: "normal", label: "正常执行（跳过已解析）" },
                { key: "force", label: "强制执行（重新解析全部）" },
              ],
              onClick: ({ key }) => run(key === "force"),
            }}
          >
            <Button loading={loading}>重新解析</Button>
          </Dropdown>
          <Paragraph type="secondary" style={{ margin: 0 }}>
            对已登记文件重新调用 MinerU 解析，产物写入 <code>data/parsed/</code>。
          </Paragraph>
        </Space>
      </Card>

      <ParsedTable refreshKey={refreshKey} />
    </Space>
  );
}
