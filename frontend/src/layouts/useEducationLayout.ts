/**
 * Adapted from PrimeTek Sakai's src/layout/composables/layout.js (MIT).
 * Copyright (c) 2018-2022 PrimeTek. See LICENSES/sakai-MIT.txt.
 * Changes: typed state, education layout, declared mobile state, no theme configurator.
 */
import { reactive } from "vue";
const layoutState = reactive({ staticMenuInactive: false, mobileMenuActive: false });
export function useEducationLayout() {
  const isDesktop = () => window.innerWidth > 991;
  const hideMobileMenu = () => { layoutState.mobileMenuActive = false; };
  const toggleMenu = () => {
    if (isDesktop()) layoutState.staticMenuInactive = !layoutState.staticMenuInactive;
    else layoutState.mobileMenuActive = !layoutState.mobileMenuActive;
  };
  return { layoutState, toggleMenu, hideMobileMenu, isDesktop };
}
