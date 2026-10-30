import { Fingerprint, ScanLine } from "lucide-react";

/** Atmospheric branding, independent of the actual published script catalog. */
export function ArchiveArtwork() {
  return (
    <div className="archive-artwork" aria-hidden="true">
      <div className="scene-focus">
        <span />
        <span />
        <span />
        <span />
      </div>
      <div className="scene-caption">
        <ScanLine size={15} />
        <span>迷雾之下 · 真相未明</span>
      </div>
      <div className="testimony-note">
        <div className="testimony-label">
          <Fingerprint size={21} />
          <span>关于证词 / TESTIMONY</span>
        </div>
        <p>
          每一句证词，
          <br />
          都有待核实。
        </p>
        <div className="testimony-rule">
          <span />
          <span />
          <span />
        </div>
        <span className="testimony-stamp">请勿轻信</span>
      </div>
    </div>
  );
}
