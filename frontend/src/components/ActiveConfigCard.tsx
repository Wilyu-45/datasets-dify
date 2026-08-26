/**
 * 当前激活配置方案卡片（2026-08 新增）。
 *
 * 展示正在使用的配置方案（知识库 ID + 切分策略 + 关键参数），
 * 配置中心修改并激活后，其他页面刷新即可看到最新配置 ——
 * 实现「其他网页根据配置的改变而改变」。
 */
import { Card, Descriptions, Empty, Skeleton, Space, Tag, Typography } from "antd";
import { useEffect, useState } from "react";
import { getActiveConfig, type ConfigProfile } from "../api/client";

const { Text } = Typography;

export default function ActiveConfigCard() {
  const [profile, setProfile] = useState<ConfigProfile | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    getActiveConfig()
      .then((r) => setProfile(r.profile))
      .catch(() => setProfile(null))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <Card size="small" title="当前配置">
        <Skeleton active paragraph={{ rows: 1 }} />
      </Card>
    );
  }

  if (!profile) {
    return (
      <Card size="small" title="当前配置">
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={
            <Text type="secondary" style={{ fontSize: 12 }}>
              尚未激活配置方案（请到「配置中心」配置并激活）
            </Text>
          }
        />
      </Card>
    );
  }

  return (
    <Card
      size="small"
      title={
        <Space>
          <span>当前配置</span>
          <Tag color="green">{profile.name}</Tag>
        </Space>
      }
    >
      <Descriptions size="small" column={{ xs: 1, md: 3 }}>
        <Descriptions.Item label="知识库 ID">
          <Text style={{ wordBreak: "break-all" }}>
            {String(profile.config.dify_dataset_id || "（未设置）")}
          </Text>
        </Descriptions.Item>
        <Descriptions.Item label="切分策略">
          {String(profile.config.chunk_strategy || "structure")}
        </Descriptions.Item>
        <Descriptions.Item label="目标字符数">
          {String(profile.config.chunk_target_chars ?? "-")}
        </Descriptions.Item>
      </Descriptions>
    </Card>
  );
}
