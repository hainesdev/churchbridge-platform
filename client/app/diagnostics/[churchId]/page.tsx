import { DiagnosticsDashboard } from '@/components/DiagnosticsDashboard';

export default async function DiagnosticsPage({
  params,
}: {
  params: Promise<{ churchId: string }>;
}) {
  const { churchId } = await params;
  return <DiagnosticsDashboard churchId={churchId} />;
}
