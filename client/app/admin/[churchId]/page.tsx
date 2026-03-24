import { SoundboardAdmin } from '@/components/SoundboardAdmin';

export default async function AdminPage({
  params,
}: {
  params: Promise<{ churchId: string }>;
}) {
  const { churchId } = await params;
  return (
    <main className="min-h-screen bg-gray-900 flex items-center justify-center p-4">
      <SoundboardAdmin churchId={churchId} />
    </main>
  );
}
