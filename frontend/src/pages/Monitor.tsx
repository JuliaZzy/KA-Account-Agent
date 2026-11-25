import { useDesk } from "../desk";
import { PageHead } from "../ui";

export function MonitorPage() {
  const desk = useDesk();
  const orgId = desk.user?.distributor_id ?? "";
  const src = orgId
    ? `/grafana/d/ka-monitor/monitor?orgId=1&kiosk&theme=light&var-org_id=${encodeURIComponent(orgId)}`
    : "";

  return (
    <>
      <PageHead
        title="监控台"
        caption="Prometheus 每 30 秒拉一次账本。这里看这个经销商最近的新建、失败、完成时间和排队。单条 run 在 Run data。"
      />
      {!orgId && <div className="empty">先在左侧选一个用户。</div>}
      {src && <iframe className="monitor-frame" title="监控" src={src} />}
    </>
  );
}
