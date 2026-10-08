// One authority for both document validation and rendered style semantics.
// Palette tables, contrast calculation and inheritance stay private.
export {
  GUI_STYLE_CONTRACT,
  guiValidateTheme,
  guiValidateAppearance,
  guiStyleVariables,
  guiAppearanceStyle,
  guiStyleAliases,
  guiModuleAppearanceVariables,
} from './theme.mjs';
