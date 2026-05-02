import { LandingHero } from "../components/landing/LandingHero";
import { LandingFeatures } from "../components/landing/LandingFeatures";
import { LandingManifesto } from "../components/landing/LandingManifesto";
import { LandingArchitecture } from "../components/landing/LandingArchitecture";
import { LandingFooter } from "../components/landing/LandingFooter";
import { CustomCursor } from "../components/landing/CustomCursor";

export default function LandingPage() {
  return (
    <div className="relative min-h-screen">
      <CustomCursor />
      <LandingHero />
      <LandingFeatures />
      <LandingManifesto />
      <LandingArchitecture />
      <LandingFooter />
    </div>
  );
}
