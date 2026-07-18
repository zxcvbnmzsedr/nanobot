import { Check, MessageCircleMore } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  ChannelLogo,
  ChannelStatusBadge,
  channelDisplayName,
  channelSetup,
  channelStatusLabel,
} from "@/components/settings/channels/ChannelIdentity";
import { WeixinConnectFlow } from "@/components/settings/channels/ChannelQrConnectFlow";
import { ChannelSetupLinks } from "@/components/settings/channels/ChannelSetupParts";
import type {
  NanobotChannelInstanceInfo,
  NanobotFeatureInfo,
  NanobotFeaturesPayload,
} from "@/lib/types";

export function WeixinAccountsPanel({
  token,
  feature,
  showBrandLogos,
  chatAppsDocsUrl,
  onFeaturesUpdate,
}: {
  token: string;
  feature: NanobotFeatureInfo;
  showBrandLogos: boolean;
  chatAppsDocsUrl?: string;
  onFeaturesUpdate: (payload: NanobotFeaturesPayload) => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const instances = weixinInstances(feature);
  const setup = channelSetup(feature);
  const connectedCount = instances.filter((instance) => instance.configured).length;

  return (
    <aside className="min-h-full rounded-[20px] border border-border/80 bg-background p-5 shadow-none">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <ChannelLogo feature={feature} showBrandLogos={showBrandLogos} />
          <div className="min-w-0 flex-1">
            <h3 className="truncate text-[18px] font-semibold leading-6 text-foreground">
              {channelDisplayName(feature, t)}
            </h3>
            <p className="mt-1 text-[13px] leading-5 text-muted-foreground">
              {connectedCount === 1
                ? tx("settings.channels.oneWeixinAccount", "1 WeChat account connected")
                : tx(
                    "settings.channels.manyWeixinAccounts",
                    `${connectedCount} WeChat accounts connected`,
                  )}
            </p>
          </div>
        </div>
        <ChannelStatusBadge>{channelStatusLabel(feature, tx)}</ChannelStatusBadge>
      </div>

      <div className="mt-5 rounded-[16px] border border-border/70 bg-background px-4 py-4">
        <p className="text-[12.5px] leading-5 text-muted-foreground">
          {t("settings.channels.items.weixin.setup.summary", {
            defaultValue: "WeChat uses QR login and saves each account state locally.",
          })}
        </p>
        <ChannelSetupLinks
          feature={feature}
          setup={setup}
          chatAppsDocsUrl={chatAppsDocsUrl}
        />
      </div>

      <div className="mt-5 space-y-3">
        {instances.map((instance, index) => (
          <section
            key={instance.id}
            className="rounded-[16px] border border-border/65 bg-background px-4 py-4"
          >
            <div className="flex min-w-0 items-center gap-3">
              <span className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-emerald-500/10 text-emerald-700 dark:text-emerald-200">
                <MessageCircleMore className="h-5 w-5" aria-hidden />
              </span>
              <div className="min-w-0 flex-1">
                <div className="truncate text-[13px] font-semibold text-foreground">
                  {instance.name || `${tx("settings.channels.weixinAccount", "WeChat account")} ${index + 1}`}
                </div>
                <div className="mt-0.5 flex items-center gap-1.5 text-[11.5px] text-muted-foreground">
                  {instance.configured ? <Check className="h-3.5 w-3.5 text-emerald-600" aria-hidden /> : null}
                  {instance.configured
                    ? tx("settings.channels.weixinConfigured", "Connected")
                    : tx("settings.channels.weixinNotConfigured", "Needs connection")}
                </div>
              </div>
            </div>
            <WeixinConnectFlow
              token={token}
              instanceId={instance.id}
              mode="replace"
              force={instance.configured}
              idleLabel={instance.configured
                ? tx("settings.channels.reconnectWeixin", "Reconnect")
                : t("settings.channels.items.weixin.setup.primaryAction", {
                    defaultValue: tx("settings.channels.connect", "Connect"),
                  })}
              onFeaturesUpdate={onFeaturesUpdate}
            />
          </section>
        ))}
      </div>

      <section className="mt-4 rounded-[16px] border border-border/70 bg-background px-4 py-4">
        <div className="text-[13px] font-semibold text-foreground">
          {tx("settings.channels.addWeixinAccount", "Add another WeChat account")}
        </div>
        <p className="mt-1 text-[12.5px] leading-5 text-muted-foreground">
          {tx(
            "settings.channels.addWeixinAccountHint",
            "Each account receives and replies independently while sharing the institution identity.",
          )}
        </p>
        <WeixinConnectFlow
          key="create-weixin-account"
          token={token}
          mode="create"
          idleLabel={tx("settings.channels.addWeixinAccount", "Add WeChat account")}
          onFeaturesUpdate={onFeaturesUpdate}
        />
      </section>
    </aside>
  );
}

function weixinInstances(feature: NanobotFeatureInfo): NanobotChannelInstanceInfo[] {
  if (feature.instances?.length) return feature.instances;
  return [{
    id: "default",
    name: "WeChat account",
    enabled: feature.enabled,
    configured: Boolean(feature.configured),
  }];
}
