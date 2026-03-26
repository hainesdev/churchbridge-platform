import { MobileTest } from '@/components/MobileTest';

export default async function TestPage({
  params,
}: {
  params: Promise<{ churchId: string }>;
}) {
  const { churchId } = await params;
  return <MobileTest churchId={churchId} />;
}
