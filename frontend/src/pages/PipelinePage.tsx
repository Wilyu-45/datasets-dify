import { Alert, Button, Card, Dropdown, message, Space, Typography } from "antd";
import { useEffect, useState } from "react";
import BatchFileUpload from "../components/BatchFileUpload";
import ManifestTable from "../components/ManifestTable";
import {
  getActiveConfig,
  triggerChunk,
  triggerDifyUpload,
  triggerParse,
  triggerScan,
  type BatchUploadResponse,
  type ChunkReport,
  type DifyUploadReport,
  type ParseReport,
  type PipelineReport,
  type ScanReport,
} from "../api/client";
import DifyReportTable from "../components/DifyReportTable";

const { Title, Paragraph } = Typography;

/** 分环节单独处理的四个环节（数组顺序即按钮展示顺序） */
type StepKey = "scan" | "parse" | "chunk" | "dify";

const STEP_LABELS: Record<StepKey, string> = {
  scan: "扫描登记",
  parse: "解析",
  chunk: "切分",
  dify: "入库",
};

const STEP_ORDER: StepKey[] = ["scan", "parse", "chunk", "dify"];

interface Props {
  /** ★ 2026-08：跳转配置中心 */
  onOpenConfig?: () => void;
}

export default function PipelinePage({ onOpenConfig }: Props) {
  const [refreshKey, setRefreshKey] = useState(0);
  const [lastReport, setLastReport] = useState<PipelineReport | null>(null);
  // ★ 2026-08-31：当前激活配置方案（仅文档处理配置可用于上传入库；
  // 两套配置各自独立激活，上传页取 upload 类型的激活方案）
  const [activeProfileId, setActiveProfileId] = useState<string | undefined>();
  const [activeProfileName, setActiveProfileName] = useState<string | undefined>();
  // 未激活文档处理配置时给出提示
  const [activeIsWebscrape, setActiveIsWebscrape] = useState(false);
  // ★ 2026-09 生产部署改造：分环节单独处理（单步触发的报告覆盖整批流水线对应阶段）
  const [busyStep, setBusyStep] = useState<StepKey | null>(null);
  const [scanReport, setScanReport] = useState<ScanReport | null>(null);
  const [stageParse, setStageParse] = useState<ParseReport | null>(null);
  const [stageChunk, setStageChunk] = useState<ChunkReport | null>(null);
  const [stageDify, setStageDify] = useState<DifyUploadReport | null>(null);

  // 加载当前激活配置方案（上传处理将使用它；上传页只用文档处理配置）
  useEffect(() => {
    getActiveConfig()
      .then((r) => {
        const isWeb = (r.profile?.type ?? "upload") === "webscrape";
        setActiveIsWebscrape(isWeb || !r.profile);
        if (isWeb || !r.profile) {
          setActiveProfileId(undefined);
          setActiveProfileName(undefined);
        } else {
          setActiveProfileId(r.profile?.id);
          setActiveProfileName(r.profile?.name);
        }
      })
      .catch(() => {
        setActiveProfileId(undefined);
        setActiveProfileName(undefined);
      });
  }, []);

  // 批量上传后刷新 manifest；上传即自动入库，整批 pipeline 报告也会回填
  const onAfterBatchUpload = (r: BatchUploadResponse) => {
    setRefreshKey((k) => k + 1);
    if (r.pipeline) {
      setLastReport(r.pipeline);
      // 整批结果优先：清掉之前单步触发留下的覆盖报告
      setScanReport(null);
      setStageParse(null);
      setStageChunk(null);
      setStageDify(null);
    }
  };

  // 分环节单独触发：dry_run=false，force 由弹层选择；成功后刷新清单并回填该阶段报告
  const runStep = async (step: StepKey, force: boolean) => {
    setBusyStep(step);
    try {
      if (step === "scan") {
        const r = await triggerScan(false, force);
        setScanReport(r);
        message.success(
          `扫描登记完成：新增 ${r.new}，重命名 ${r.renamed}，跳过 ${r.skipped_done}，失败 ${r.failed}`
        );
      } else if (step === "parse") {
        const r = await triggerParse(false, force);
        setStageParse(r);
        message.success(
          `解析完成：成功 ${r.parsed}，跳过 ${r.skipped_done}，失败 ${r.failed}`
        );
      } else if (step === "chunk") {
        const r = await triggerChunk(false, force);
        setStageChunk(r);
        message.success(
          `切分完成：成功 ${r.chunked}，跳过 ${r.skipped_done}，失败 ${r.failed}`
        );
      } else {
        const r = await triggerDifyUpload(false, force);
        setStageDify(r);
        message.success(
          `入库完成：上传 ${r.uploaded}，跳过 ${r.skipped_done}，失败 ${r.failed}`
        );
      }
      setRefreshKey((k) => k + 1);
    } catch (e) {
      message.error(`${STEP_LABELS[step]}失败：${(e as Error).message}`);
    } finally {
      setBusyStep(null);
    }
  };

  // 单步触发的报告优先展示，其次回退到整批流水线报告
  const parseReport: ParseReport | null =
    stageParse ?? lastReport?.parse ?? null;
  const chunkReport: ChunkReport | null =
    stageChunk ?? lastReport?.chunk ?? null;
  const difyReport: DifyUploadReport | null =
    stageDify ?? lastReport?.dify ?? null;

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <div>
        <Title level={4} style={{ margin: 0 }}>
          入库工作台
        </Title>
        <Paragraph type="secondary" style={{ marginTop: 4, marginBottom: 0 }}>
          上传文档后自动登记并全流程处理：
          <code>① 解析</code> → <code>② 切分</code> → <code>③ 入库</code>。
          处理前请先在<strong>配置中心</strong>配置<strong>文档处理配置</strong>
          （知识库 ID 与切分策略）并激活；网站抓取配置不用于本页。
          下方可查看入库明细与文件清单。
        </Paragraph>
      </div>
      {activeIsWebscrape && (
        <Alert
          type="warning"
          showIcon
          message="尚未激活「文档处理配置」，上传入库不可用"
          description="请先到配置中心创建并激活文档处理配置（与网站抓取配置各自独立激活，互不影响）。当前激活的网站抓取配置仅用于网站抓取页。"
          action={
            <Button type="link" size="small" onClick={onOpenConfig}>
              去配置中心激活文档处理配置
            </Button>
          }
        />
      )}
      <Card size="small" title="分环节处理">
        <Space direction="vertical" size="small" style={{ width: "100%" }}>
          <Space wrap>
            {STEP_ORDER.map((step) => (
              <Dropdown
                key={step}
                trigger={["click"]}
                menu={{
                  items: [
                    { key: "normal", label: "正常执行（跳过已完成）" },
                    { key: "force", label: "强制执行（忽略已完成标记）" },
                  ],
                  onClick: ({ key }) => runStep(step, key === "force"),
                }}
              >
                <Button
                  loading={busyStep === step}
                  disabled={busyStep !== null && busyStep !== step}
                >
                  {STEP_LABELS[step]}
                </Button>
              </Dropdown>
            ))}
          </Space>
          <Paragraph type="secondary" style={{ margin: 0 }}>
            每个环节均可单独触发：直接点击按默认规则执行（跳过已完成）；点击后在弹层选择
            「强制执行」可忽略已完成标记重跑。需按{" "}
            <code>扫描登记 → 解析 → 切分 → 入库</code> 顺序执行。
          </Paragraph>
          {scanReport && (
            <Alert
              type="info"
              showIcon
              message={`最近一次扫描登记：新增 ${scanReport.new}，重命名 ${scanReport.renamed}，跳过 ${scanReport.skipped_done}，磁盘缺失 ${scanReport.missing_on_disk}，失败 ${scanReport.failed}`}
            />
          )}
        </Space>
      </Card>

      <BatchFileUpload
        onAfterUpload={onAfterBatchUpload}
        profileId={activeProfileId}
        profileName={activeProfileName}
        onOpenConfig={onOpenConfig}
      />

      {difyReport && (
        <Space direction="vertical" size="small" style={{ width: "100%" }}>
          <Title level={5} style={{ margin: 0 }}>
            入库阶段明细
          </Title>
          <DifyReportTable lastReport={difyReport} />
        </Space>
      )}

      <ManifestTable
        refreshKey={refreshKey}
        parseReport={parseReport}
        chunkReport={chunkReport}
        difyReport={difyReport}
      />
    </Space>
  );
}
