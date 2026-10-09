{{- define "platform.labels" -}}
app.kubernetes.io/part-of: mcp-gateway
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end }}

{{- define "platform.selector" -}}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/name: {{ .name }}
{{- end }}

{{- define "platform.image" -}}
{{ .root.Values.images.registry }}{{ .name }}:{{ .root.Values.images.tag }}
{{- end }}

{{- define "platform.podSecurity" -}}
runAsNonRoot: true
seccompProfile:
  type: RuntimeDefault
{{- end }}

{{- define "platform.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end }}

{{- define "platform.url" -}}
{{ .Values.scheme }}://{{ .Values.publicHost }}
{{- end }}
