using './main.bicep'

param prefix = 'qproofdev'
param deployWorkloads = false
param runSubmissionEnabled = false
param jobTriggerType = 'Manual'
param controlMinReplicas = 0
