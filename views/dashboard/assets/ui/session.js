/* One cached read of the user's own profile (nickname, demo flag, whether a model is configured). */
import { communityApi } from "/assets/ui/api.js";

let cache = null, stamp = 0, pending = null;

export function loadProfile({force = false} = {}) {
  if (!force && cache && Date.now() - stamp < 60000) return Promise.resolve(cache);
  pending ??= communityApi.profile().then(data => {
    cache = {
      nickname: String(data.nickname || ""),
      demo: data.demo === true,
      modelReady: Boolean(data.has_key && data.profile?.model),
      agentName: String(data.agent_name || "久留美"),
    };
    stamp = Date.now();
    return cache;
  }).finally(() => { pending = null; });
  return pending;
}

export const cachedProfile = () => cache;