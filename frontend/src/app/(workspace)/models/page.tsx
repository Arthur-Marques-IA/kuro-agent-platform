import type { Metadata } from "next";
import { ModelsIndex } from "@/components/models/models-index";
import { MODEL_CREDENTIALS_TITLE } from "@/lib/model-credentials";

export const metadata: Metadata = { title: MODEL_CREDENTIALS_TITLE };

export default function ModelsPage() {
  return <ModelsIndex />;
}
