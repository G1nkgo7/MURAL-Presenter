import { PaperPage } from "../PaperPage";
import { createPageMetadata } from "../metadata";

export const metadata = createPageMetadata(
  "MURAL-Presenter — Research manuscript",
  "Read the local working manuscript for MURAL-Presenter.",
  "en_US",
);

export default function EnglishPaper() {
  return <PaperPage language="en" />;
}
