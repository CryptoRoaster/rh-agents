import { NextResponse, type NextRequest } from "next/server";
import { cockpitTarget } from "@/lib/cockpit-proxy";
export const dynamic = "force-dynamic";

// GET is the only exported method, so every other method is refused with 405.
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const url = cockpitTarget((await params).path, request.nextUrl.searchParams);
  if (url === null)
    return NextResponse.json({ detail: "Not found" }, { status: 404 });
  try {
    const response = await fetch(url, {
      cache: "no-store",
      signal: AbortSignal.timeout(8000),
      redirect: "error",
    });
    const body = await response.text();
    return new NextResponse(body, {
      status: response.status,
      headers: {
        "content-type": "application/json",
        "cache-control": "no-store",
      },
    });
  } catch {
    return NextResponse.json(
      { detail: "Backend unavailable" },
      { status: 502 },
    );
  }
}
