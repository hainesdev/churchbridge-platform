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
  const displayMode =
    mode === 'lowerthird' ? 'lowerthird' :
    mode === 'spanish'    ? 'spanish'    :
    mode === 'bilingual'  ? 'bilingual'  :
    'full';

  return (
    <div className="h-screen">
      <TranslationDisplay churchId={churchId} mode={displayMode} />
    </div>
  );
}
