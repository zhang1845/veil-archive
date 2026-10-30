import { Suspense } from "react";
import { PlayRoom } from "@/components/play/PlayRoom";

export default function PlayPage() {
  return (
    <Suspense fallback={null}>
      <PlayRoom />
    </Suspense>
  );
}
