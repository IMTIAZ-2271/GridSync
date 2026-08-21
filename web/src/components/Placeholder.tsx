/** Stub page. Names the endpoint it will read so the scaffold is self-documenting. */
export default function Placeholder({
  title,
  note,
}: {
  title: string;
  note: string;
}) {
  return (
    <div className="rounded-lg border border-dashed border-slate-300 bg-white p-8">
      <h1 className="text-xl font-semibold">{title}</h1>
      <p className="mt-2 max-w-prose text-sm text-slate-500">{note}</p>
      <p className="mt-4 text-xs font-medium tracking-wide text-slate-400 uppercase">
        Not built yet
      </p>
    </div>
  );
}
