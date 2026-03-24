import { TranslationDisplay } from '@/components/TranslationDisplay';

export default async function DisplayPage({
  params,
  searchParams,
}: {
  params: Promise<{ churchId: string }>;
  searchParams: Promise<{ mode?: string }>;
}) {
  const { churchId } = await params;
  const { mode } = await searchParams;
  const displayMode = mode === 'lowerthird' ? 'lowerthird' : 'full';

  return (
    <div className={`${displayMode === 'lowerthird' ? 'h-screen' : 'h-screen'}`}>
      <TranslationDisplay churchId={churchId} mode={displayMode} />
    </div>
  );
}
