export default function Home() {
  return (
    <main className="min-h-screen bg-gray-950 text-white flex items-center justify-center p-8">
      <div className="max-w-md w-full space-y-6">
        <h1 className="text-2xl font-bold text-center">ChurchBridge AI</h1>
        <p className="text-gray-400 text-center text-sm">
          Real-time Spanish → English translation for live church services.
        </p>
        <div className="space-y-3">
          <a
            href="/admin/default"
            className="block w-full py-3 px-4 bg-green-700 hover:bg-green-600 rounded-lg text-center font-medium transition-colors"
          >
            Soundboard Admin
          </a>
          <a
            href="/display/default"
            className="block w-full py-3 px-4 bg-blue-700 hover:bg-blue-600 rounded-lg text-center font-medium transition-colors"
          >
            Sanctuary Display
          </a>
          <a
            href="/display/default?mode=lowerthird"
            className="block w-full py-3 px-4 bg-gray-700 hover:bg-gray-600 rounded-lg text-center font-medium transition-colors"
          >
            Lower Thirds (OBS)
          </a>
          <a
            href="/listen/default"
            className="block w-full py-3 px-4 bg-purple-700 hover:bg-purple-600 rounded-lg text-center font-medium transition-colors"
          >
            Mobile Listener
          </a>
          <a
            href="/test/default"
            className="block w-full py-3 px-4 bg-gray-700 hover:bg-gray-600 rounded-lg text-center font-medium transition-colors"
          >
            Translation Test
          </a>
        </div>
      </div>
    </main>
  );
}
