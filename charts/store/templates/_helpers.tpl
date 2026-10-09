{{- define "store.labels" -}}
app.kubernetes.io/part-of: woo-store
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end }}

{{- define "store.url" -}}
{{ .Values.store.scheme }}://{{ .Values.store.host }}
{{- end }}

{{- define "store.podSecurity" -}}
seccompProfile:
  type: RuntimeDefault
{{- end }}

{{- define "store.containerSecurity" -}}
allowPrivilegeEscalation: false
capabilities:
  drop: ["ALL"]
{{- end }}
