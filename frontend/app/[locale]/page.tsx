import { useTranslations } from "next-intl";
import { setRequestLocale } from "next-intl/server";
import { use } from "react";

import type { Locale } from "next-intl";

type HomePageProps = {
  params: Promise<{ locale: Locale }>;
};

export default function HomePage({ params }: HomePageProps) {
  const { locale } = use(params);
  setRequestLocale(locale);
  const t = useTranslations("HomePage");

  return (
    <main className="mx-auto flex min-h-dvh max-w-3xl flex-col justify-center gap-4 px-4 py-12">
      <h1 className="text-foreground text-3xl font-semibold">{t("title")}</h1>
      <p className="text-muted-foreground text-lg">{t("tagline")}</p>
      <p className="border-border bg-surface text-foreground rounded-md border px-4 py-3 text-sm">
        {t("status")}
      </p>
    </main>
  );
}
