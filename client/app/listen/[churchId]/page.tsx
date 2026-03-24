import { MobileListener } from '@/components/MobileListener';

export default async function ListenPage({
  params,
}: {
  params: Promise<{ churchId: string }>;
}) {
  const { churchId } = await params;
  return <MobileListener churchId={churchId} />;
}
