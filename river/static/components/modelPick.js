// The Model choice of the launch dialog (#5238). An agent of the multi family (OpenCode, Pi) can know hundreds of
// provider/model texts: OpenCode's `opencode models` lists about 500. A select that long is no choice. When the list
// is long and the adapter names its own models (pick: true, from DESCRIBE['models']), the select offers those and the
// current choice, and "Other model…" opens a field that finds any of the rest by typing.

export const LONG = 30;  // more models than this, with some marked pick: the short select and the search field
export const OTHER = "__other__";  // the select value of "Other model…"

// {shown, all}: the models the select lists, in their order, and the models the search field finds ([] when the
// select lists every model). current: the model to preselect, kept in the select when it is not a pick.
export function modelChoices(models, current) {
  const ms = models || [];
  if (ms.length <= LONG || !ms.some(m => m.pick)) return { shown: ms, all: [] };
  return { shown: ms.filter(m => m.pick || m.name === current), all: ms };
}
