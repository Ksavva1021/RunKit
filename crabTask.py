import copy
import datetime
import json
import os
import re
import shutil
import sys
import tarfile
import time

if __name__ == "__main__":
  file_dir = os.path.dirname(os.path.abspath(__file__))
  sys.path.append(os.path.dirname(file_dir))
  __package__ = 'RunKit'

from .crabTaskStatus import CrabTaskStatus, Status, JobStatus, LogEntryParser, StatusOnScheduler, StatusOnServer
from .run_tools import PsCallError, ps_call, natural_sort, timestamp_str
from .grid_tools import get_voms_proxy_info, lfn_to_pfn, gfal_copy_safe, gfal_ls_safe
from .envToJson import get_cmsenv
from .getFileRunLumi import getFileRunLumi

class Task:
  _taskCfgProperties = [
    'cmsswPython', 'params', 'splitting', 'unitsPerJob', 'scriptExe', 'filesToTransfer',
    'lumiMask', 'maxMemory', 'numCores', 'inputDBS', 'allowNonValid',
    'vomsGroup', 'vomsRole', 'blacklist', 'whitelist', 'whitelistFinalRecovery', 'dryrun',
    'maxRecoveryCount', 'targetOutputFileSize', 'ignoreFiles', 'ignoreLocality', 'crabType'
  ]

  _taskCfgPrivateProperties = [
    'name', 'inputDataset', 'recoveryIndex', 'taskIds', 'lastJobStatusUpdate', 'outputs', 'startDate', 'endDate'
  ]

  inputLumiMaskJsonName = 'inputLumis'
  crabOperationTimeout = 10 * 60
  dasOperationTimeout = 10 * 60

  def __init__(self):
    self.taskStatus = CrabTaskStatus()
    self.workArea = ''
    self.cfgPath = ''
    self.statusPath = ''
    self.name = ''
    self.inputDataset = ''
    self.cmsswPython = ''
    self.params = {}
    self.splitting = ''
    self.unitsPerJob = -1
    self.scriptExe = ''
    self.filesToTransfer = []
    self.outputs = []
    self.lumiMask = ''
    self.maxMemory = -1
    self.numCores = -1
    self.inputDBS = ''
    self.allowNonValid = False
    self.vomsGroup = ''
    self.vomsRole = ''
    self.blacklist = []
    self.whitelist = []
    self.whitelistFinalRecovery = []
    self.ignoreLocality = False
    self.dryrun = False
    self.recoveryIndex = 0
    self.maxRecoveryCount = 0
    self.targetOutputFileSize = 0
    self.ignoreFiles = []
    self.jobInputFiles = None
    self.datasetFiles = None
    self.fileRunLumi = None
    self.fileRepresentativeRunLumi = None
    self.taskIds = {}
    self.lastJobStatusUpdate = -1.
    self.cmsswEnv = None
    self.singularity_cmd = os.environ.get('CMSSW_SINGULARITY', None)
    self.gridJobs = None
    self.crabType = ''
    self.processedFilesCache = None
    self.vomsToken = None
    self.startDate = ''
    self.endDate = ''

  def checkConfigurationValidity(self):
    def check(cond, prop):
      if not cond:
        raise RuntimeError(f'{self.name}: Configuration error: {prop} is not correctly set.')
    def check_len(prop):
      check(len(getattr(self, prop)) > 0, prop)

    for prop in [ 'cmsswPython', 'splitting', 'inputDBS', 'name', 'inputDataset' ]:
      check_len(prop)
    check(self.unitsPerJob > 0, 'unitsPerJob')
    check(self.maxMemory > 0, 'maxMemory')
    check(self.numCores > 0, 'numCores')

  def _setFromCfg(self, pName, cfg, add=False):
    if pName in cfg:
      pType = type(getattr(self, pName))
      pValue = copy.deepcopy(cfg[pName])
      if pType == float and type(pValue) == int:
        pValue = float(pValue)
      if pType != type(pValue):
        raise RuntimeError(f'{self.name}: inconsistent config type for "{pName}". cfg value = "{pValue}"')
      if add:
        if pType == list:
          x = list(set(getattr(self, pName) + pValue))
          setattr(self, pName, x)
        elif pType == dict:
          getattr(self, pName).update(pValue)
        else:
          setattr(self, pName, pValue)
      else:
        setattr(self, pName, pValue)

  def saveCfg(self):
    cfg = { }
    for pName in Task._taskCfgPrivateProperties:
      cfg[pName] = getattr(self, pName)
    for pName in Task._taskCfgProperties:
      cfg[pName] = getattr(self, pName)
    with open(self.cfgPath, 'w') as f:
      json.dump(cfg, f, indent=2)

  def saveStatus(self):
    with open(self.statusPath, 'w') as f:
      f.write(self.taskStatus.to_json())

  def requestName(self, recoveryIndex=None):
    name = self.name
    if recoveryIndex is None:
      recoveryIndex = self.recoveryIndex
    if recoveryIndex > 0:
      name += f'_recovery_{recoveryIndex}'
    return name

  def getOutputs(self, forceUpdate=False):
    if len(self.outputs) == 0 or forceUpdate:
      self.outputs = []
      if 'outputs' not in self.params or len(self.params['outputs']) == 0:
        raise RuntimeError(f'{self.name}: no outputs are defined.')
      for output in self.params['outputs']:
        desc = { 'file': output['file'] }
        desc['name'], desc['ext'] = os.path.splitext(output['file'])
        for destName in [ 'crabOutput', 'finalOutput' ]:
          dest = output[destName]
          if dest.startswith('T'):
            server, lfn = dest.split(':')
            task_lfn = os.path.join(lfn, self.name)
            dest = lfn_to_pfn(server, task_lfn)
          desc[destName] = dest
        if 'skimCfg' in output:
          desc['skimCfg'] = output['skimCfg']
          desc['skimSetup'] = output['skimSetup']
          if 'skimSetupFailed' in output:
            desc['skimSetupFailed'] = output['skimSetupFailed']
        self.outputs.append(desc)
    return self.outputs

  def getParams(self, appendDatasetFiles=True):
    params = [ f'{key}={value}' for key,value in self.params.items() if key != 'outputs' ]
    for output in self.getOutputs():
      output_list = [ output['file'], output['crabOutput'] ]
      if 'skimCfg' in output:
        output_list.append(output['skimCfg'])
        output_list.append(output['skimSetup'])
        if 'skimSetupFailed' in output:
          output_list.append(output['skimSetupFailed'])
      output_str = 'output=' + ';'.join(output_list)
      params.append(output_str)
    if appendDatasetFiles:
      datasetFileDir, datasetFileName = os.path.split(self.getDatasetFilesPath())
      params.append(f'datasetFiles={datasetFileName}')
    return params

  def isInputDatasetLocal(self):
    return self.inputDataset.startswith('local:')

  def isInLocalRunMode(self, recoveryIndex=None):
    if recoveryIndex is None:
      recoveryIndex = self.recoveryIndex
    return self.isInputDatasetLocal() or recoveryIndex >= self.maxRecoveryCount

  def getUnitsPerJob(self):
    if self.recoveryIndex >= self.maxRecoveryCount - 1:
      return 1
    return max(self.unitsPerJob // (2 ** self.recoveryIndex), 1)

  def getSplitting(self):
    if self.recoveryIndex > 0:
      return 'FileBased'
    return self.splitting

  def getLumiMask(self):
    if self.recoveryIndex > 0:
      return os.path.join(self.workArea, f'{Task.inputLumiMaskJsonName}_{self.recoveryIndex}.json')
    return self.lumiMask

  def getMaxMemory(self):
    if self.recoveryIndex == self.maxRecoveryCount:
      return max(self.maxMemory, 4000)
    return self.maxMemory

  def getWhiteList(self):
    if self.recoveryIndex == self.maxRecoveryCount:
      return self.whitelistFinalRecovery
    return self.whitelist

  def getBlackList(self):
    return self.blacklist

  def getIgnoreLocality(self):
    if self.recoveryIndex == self.maxRecoveryCount:
      return True
    return self.ignoreLocality

  def getFilesToTransfer(self, appendDatasetFiles=True):
    if appendDatasetFiles:
      return self.filesToTransfer + [ self.getDatasetFilesPath() ]
    return self.filesToTransfer

  def getCmsswEnv(self):
    if self.cmsswEnv is None:
      cmssw_path = os.environ['DEFAULT_CMSSW_BASE']
      self.cmsswEnv = get_cmsenv(cmssw_path, crab_env=True, crab_type=self.crabType,
                                 singularity_cmd=self.singularity_cmd)
      self.cmsswEnv['X509_USER_PROXY'] = os.environ['X509_USER_PROXY']
      self.cmsswEnv['HOME'] = os.environ['HOME'] if 'HOME' in os.environ else self.workArea
      if 'KRB5CCNAME' in os.environ:
        self.cmsswEnv['KRB5CCNAME'] = os.environ['KRB5CCNAME']
    return self.cmsswEnv

  def getVomsToken(self):
    if self.vomsToken is None:
      self.vomsToken = get_voms_proxy_info()['path']
    return self.vomsToken

  def getDatasetFilesPath(self):
    return os.path.join(self.workArea, 'dataset_files.json')

  def getDatasetFiles(self):
    if self.datasetFiles is None:
      datasetFilesPath = self.getDatasetFilesPath()
      if os.path.exists(datasetFilesPath):
        with open(datasetFilesPath, 'r') as f:
          self.datasetFiles = json.load(f)
      else:
        if self.isInputDatasetLocal():
          print(f'{self.name}: Gathering dataset files ...')
          ds_path = self.inputDataset[len('local:'):]
          if not os.path.exists(ds_path):
            raise RuntimeError(f'{self.name}: unable to find local dataset path "{ds_path}"')
          self.datasetFiles = {}
          all_files = []
          for subdir, dirs, files in os.walk(ds_path):
            for file in files:
              if file.endswith('.root') and not file.startswith('.'):
                all_files.append('file:' + os.path.join(subdir, file))
          for file_id, file_path in enumerate(natural_sort(all_files)):
            self.datasetFiles[file_path] = file_id
        else:
          self.datasetFiles = {}
          for file_id, file in enumerate(natural_sort(self.getFileRunLumi().keys())):
            self.datasetFiles[file] = file_id
        with open(datasetFilesPath, 'w') as f:
          json.dump(self.datasetFiles, f, indent=2)
      if len(self.datasetFiles) == 0:
        raise RuntimeError(f'{self.name}: empty dataset {self.inputDataset}')
    return self.datasetFiles

  def getDatasetFileById(self, file_id):
    if type(file_id) is str:
      file_id = int(file_id)
    for file, fileId in self.getDatasetFiles().items():
      if fileId == file_id:
        return file
    raise RuntimeError(f'{self.name}: unable to find file with id {file_id}')

  def getFileRunLumi(self):
    if self.fileRunLumi is None:
      fileRunLumiPath = os.path.join(self.workArea, 'file_run_lumi.json')
      if not os.path.exists(fileRunLumiPath):
        print(f'{self.name}: Gathering file->(run,lumi) correspondance ...')
        self.fileRunLumi = getFileRunLumi(self.inputDataset, inputDBS=self.inputDBS,
                                          dasOperationTimeout=Task.dasOperationTimeout)
        with open(fileRunLumiPath, 'w') as f:
          json.dump(self.fileRunLumi, f, indent=2)
      else:
        with open(fileRunLumiPath, 'r') as f:
          self.fileRunLumi = json.load(f)
    return self.fileRunLumi

  def getFileRepresentativeRunLumi(self):
    if self.fileRepresentativeRunLumi is None:
      fileRunLumi = self.getFileRunLumi()
      self.fileRepresentativeRunLumi = {}
      for file, fileRuns in fileRunLumi.items():
        def hasOverlaps(run, lumi):
          for otherFile, otherFileRuns in fileRunLumi.items():
            if otherFile != file and run in otherFileRuns and lumi in otherFileRuns[run]:
              return True
          return False
        def findFirstRepresentative():
          for fileRun, runLumis in fileRuns.items():
            for runLumi in runLumis:
              if not hasOverlaps(fileRun, runLumi):
                return (fileRun, runLumi)
          print(f"{self.name}: Unable to find representative (run, lumi) for {file}. Using the first one.")
          fileRun = next(iter(fileRuns))
          runLumi = fileRuns[fileRun][0]
          return (fileRun, runLumi)
        self.fileRepresentativeRunLumi[file] = findFirstRepresentative()
    return self.fileRepresentativeRunLumi

  def getRepresentativeLumiMask(self, files):
    lumiMask = {}
    repRunLumi = self.getFileRepresentativeRunLumi()
    for file in files:
      if file not in repRunLumi:
        raise RuntimeError(f'{self.name}: cannot find representative run-lumi for "{file}"')
      run, lumi = repRunLumi[file]
      run = str(run)
      if run not in lumiMask:
        lumiMask[run] = []
      lumiMask[run].append([lumi, lumi])
    return lumiMask

  def selectJobIds(self, jobStatuses, invert=False, recoveryIndex=None):
    jobIds = []
    for jobId, status in self.getTaskStatus(recoveryIndex).get_job_status().items():
      if (status in jobStatuses and not invert) or (status not in jobStatuses and invert):
        jobIds.append(jobId)
    return jobIds

  def getTimeSinceLastJobStatusUpdate(self):
    if self.lastJobStatusUpdate <= 0:
      return -1
    now = datetime.datetime.now()
    t = datetime.datetime.fromtimestamp(self.lastJobStatusUpdate)
    return (now - t).total_seconds() / (60 * 60)

  def getTaskStatus(self, recoveryIndex=None):
    if recoveryIndex is None:
      recoveryIndex = self.recoveryIndex
    if recoveryIndex == self.recoveryIndex:
      return self.taskStatus
    statusPath = os.path.join(self.workArea, f'status_{recoveryIndex}.json')
    with open(statusPath, 'r') as f:
      return CrabTaskStatus.from_json(f.read())

  def getTaskId(self, recoveryIndex=None):
    if recoveryIndex is None:
      recoveryIndex = self.recoveryIndex
    if recoveryIndex not in self.taskIds:
      self.taskIds[recoveryIndex] = self.getTaskStatus(recoveryIndex=recoveryIndex).task_id()
      self.saveCfg()
    return self.taskIds[recoveryIndex]

  def prepareForPostProcess(self):
    missingFiles = self.getFilesToProcess()
    if len(missingFiles) > 0:
      raise RuntimeError(f'{self.name}: missing outputs for following input files: ' + ' '.join(missingFiles))

  def postProcessOutputs(self, job_home):
    haddnanoEx_path = os.path.join(os.path.dirname(__file__), 'haddnanoEx.py')
    datasetFiles = self.getDatasetFiles()
    processedFiles = self.getProcessedFiles()
    notProcessedFiles = sorted(list(datasetFiles.keys() - processedFiles.keys()))
    for output in self.getOutputs():
      outputName = output['file']
      outputNameBase, outputExt = os.path.splitext(outputName)
      print(f'{self.name}: merging outputs for {outputName}...')

      report = {}
      report['inputDataset'] = self.inputDataset
      report['processingStart'] = self.startDate
      report['notProcessedFiles'] = notProcessedFiles
      haddInputs = {}
      file_list_path = os.path.join(job_home, 'file_list.txt')
      with open(file_list_path, 'w') as f:
        for fileName, fileDesc in processedFiles.items():
          x = fileDesc['outputs'][outputName]
          haddInputs[x] = fileName
          f.write(x + '\n')

      hadd_report_path = os.path.join(job_home, 'merge_report.json')
      cmd = [ 'python3', '-u', haddnanoEx_path, '--output-dir', output['finalOutput'], '--output-name', outputName,
             '--target-size', str(self.targetOutputFileSize), '--file-list', file_list_path, '--remote-io',
             '--work-dir', job_home, '--merge-report', hadd_report_path]
      ps_call(cmd, verbose=1)
      with open(hadd_report_path, 'r') as f:
        hadd_report = json.load(f)
      report['outputs'] = {}
      for haddOutput, inputList in hadd_report.items():
        report['outputs'][haddOutput] = []
        for haddInput in inputList:
          report['outputs'][haddOutput].append(haddInputs[haddInput])
      report['processingEnd'] = timestamp_str()

      report_file =f'prodReport_{outputNameBase}.json'
      report_tmp_path = os.path.join(job_home, report_file)
      report_final_path = os.path.join(output['finalOutput'], report_file)
      with open(report_tmp_path, 'w') as f:
        json.dump(report, f, indent=2)
      gfal_copy_safe(report_tmp_path, report_final_path, self.getVomsToken(), verbose=1)

    self.taskStatus.status = Status.PostProcessingFinished
    self.endDate = timestamp_str()
    self.saveStatus()
    self.saveCfg()

  def getPostProcessingDoneFlagFile(self):
    return os.path.join(self.workArea, 'post_processing_done.txt')

  def getGridJobDoneFlagFile(self, job_id):
    return os.path.join(self.workArea, 'grid_jobs_results', f'job_{job_id}.done')

  def hasFailedJobs(self):
    return JobStatus.failed in self.taskStatus.job_stat

  def crabArea(self, recoveryIndex=None):
    return os.path.join(self.workArea, 'crab_' + self.requestName(recoveryIndex))

  def lastCrabStatusLog(self):
    return os.path.join(self.workArea, 'lastCrabStatus.txt')

  def submit(self):
    self.getDatasetFiles()
    if self.isInLocalRunMode():
      self.taskStatus = CrabTaskStatus()
      self.taskStatus.status = Status.Submitted
      self.taskStatus.status_on_server = StatusOnServer.SUBMITTED
      self.taskStatus.status_on_scheduler = StatusOnScheduler.SUBMITTED
      for job_id in self.getGridJobs():
        self.taskStatus.details[str(job_id)] = { "State": "idle" }
      self.saveStatus()
      return True
    else:
      crabSubmitPath = os.path.join(os.path.dirname(__file__), 'crabSubmit.py')
      if self.recoveryIndex == 0:
        print(f'{self.name}: submitting ...')
      try:
        timeout = None if self.dryrun else Task.crabOperationTimeout
        ps_call(['python3', crabSubmitPath, self.workArea], timeout=timeout, env=self.getCmsswEnv(),
                singularity_cmd=self.singularity_cmd)
        self.taskStatus.status = Status.Submitted
        self.saveStatus()
      except PsCallError as e:
        crabArea = self.crabArea()
        if os.path.exists(crabArea):
          shutil.rmtree(crabArea)
        raise e
      return False

  def updateStatus(self):
    neen_local_run = False
    oldTaskStatus = self.taskStatus
    if self.isInLocalRunMode():
      self.taskStatus = CrabTaskStatus()
      self.taskStatus.status = Status.Submitted
      self.taskStatus.status_on_server = StatusOnServer.SUBMITTED
      self.taskStatus.status_on_scheduler = StatusOnScheduler.SUBMITTED
      for job_id in self.getGridJobs():
        job_flag_file = self.getGridJobDoneFlagFile(job_id)
        if os.path.exists(job_flag_file):
          with open(job_flag_file, 'r') as f:
            job_status = f.read().strip()
        else:
          job_status = "idle"
        self.taskStatus.details[str(job_id)] = { "State": job_status }
      jobIds = self.selectJobIds([JobStatus.finished], invert=True)
      if len(jobIds) == 0:
        self.taskStatus.status = Status.CrabFinished
      self.saveStatus()
      neen_local_run = self.taskStatus.status != Status.CrabFinished
    else:
      returncode, output, err = ps_call(['crab', 'status', '--json', '-d', self.crabArea()],
                                        catch_stdout=True, split='\n', timeout=Task.crabOperationTimeout,
                                        env=self.getCmsswEnv(), singularity_cmd=self.singularity_cmd)
      self.taskStatus = LogEntryParser.Parse(output)
      self.saveStatus()
      with open(self.lastCrabStatusLog(), 'w') as f:
        f.write('\n'.join(output))
      if self.taskStatus.status == Status.Unknown:
        print(f'{self.name}: {self.taskStatus.status}. Parse error: {self.taskStatus.parse_error}')
      self.getTaskId()
    now = datetime.datetime.now()
    hasUpdates = self.lastJobStatusUpdate <= 0
    if not hasUpdates:
      jobStatus = self.taskStatus.get_job_status()
      oldJobStatus = oldTaskStatus.get_job_status()
      if len(jobStatus) != len(oldJobStatus):
        hasUpdates = True
      else:
        for jobId, status in self.taskStatus.get_job_status().items():
          if jobId not in oldJobStatus or status != oldJobStatus[jobId]:
            hasUpdates = True
            break
    if hasUpdates:
      self.lastJobStatusUpdate = now.timestamp()
      self.saveCfg()
    return neen_local_run

  def recover(self):
    filesToProcess = self.getFilesToProcess()
    if len(filesToProcess) == 0:
      print(f'{self.name}: no recovery is needed. All files have been processed.')
      self.taskStatus.status = Status.CrabFinished
      self.saveStatus()
      return False

    if self.isInLocalRunMode(recoveryIndex=self.recoveryIndex+1):
      print(f'{self.name}: creating a local recovery task\nFiles to process: ' + ', '.join(filesToProcess))
      if self.recoveryIndex == self.maxRecoveryCount - 1:
        shutil.copy(self.statusPath, os.path.join(self.workArea, f'status_{self.recoveryIndex}.json'))
        self.recoveryIndex += 1
        self.jobInputFiles = None
        self.lastJobStatusUpdate = -1.
        self.saveCfg()
        self.submit()
        return True
      else:
        return self.updateStatus()

    jobIds = self.selectJobIds([JobStatus.finished], invert=True)
    lumiMask = self.getRepresentativeLumiMask(filesToProcess)
    msg = f'{self.name}: creating a recovery task. Attempt {self.recoveryIndex + 1}/{self.maxRecoveryCount}.'
    msg += '\nUnfinished job ids: ' + ', '.join(jobIds)
    msg += '\nFiles to process: ' + ', '.join(filesToProcess)
    msg += '\nRepresentative lumi mask: ' + json.dumps(lumiMask)
    print(msg)
    n_lumi = sum([ len(x) for _, x in lumiMask.items()])
    if n_lumi != len(filesToProcess):
      raise RuntimeError(f"{self.name}: number of representative lumi sections != number of files to process.")
    shutil.copy(self.statusPath, os.path.join(self.workArea, f'status_{self.recoveryIndex}.json'))
    self.recoveryIndex += 1
    self.jobInputFiles = None
    self.lastJobStatusUpdate = -1.
    with open(self.getLumiMask(), 'w') as f:
      json.dump(lumiMask, f)
    self.saveCfg()
    try:
      self.submit()
    except PsCallError as e:
      self.recoveryIndex -= 1
      self.saveCfg()
      raise e
    return False

  def gridJobsFile(self):
    return os.path.join(self.workArea, 'grid_jobs.json')

  def getGridJobs(self):
    if not self.isInLocalRunMode():
      return {}
    if self.gridJobs is None:
      if os.path.exists(self.gridJobsFile()):
        with open(self.gridJobsFile(), 'r') as f:
          self.gridJobs = { int(key) : value for key,value in json.load(f).items() }
      else:
        self.gridJobs = {}
        job_id = 0
        units_per_job = self.getUnitsPerJob()
        for file in self.getFilesToProcess():
          while True:
            if job_id not in self.gridJobs:
              self.gridJobs[job_id] = []
            if len(self.gridJobs[job_id]) < units_per_job:
              self.gridJobs[job_id].append(file)
              break
            else:
              job_id += 1
        with open(self.gridJobsFile(), 'w') as f:
          json.dump(self.gridJobs, f, indent=2)
    return self.gridJobs

  def runJobLocally(self, job_id, job_home):
    print(f'{self.name}: running job {job_id} locally in {job_home}.')
    if not os.path.exists(job_home):
      os.makedirs(job_home)

    ana_path = os.environ['ANALYSIS_PATH']
    for file in self.getFilesToTransfer(appendDatasetFiles=False):
      shutil.copy(os.path.join(ana_path, file), job_home)
    cmd = [ 'python3', os.path.join(ana_path, self.cmsswPython), f'datasetFiles={self.getDatasetFilesPath()}',
            'writePSet=True', 'mustProcessAllInputs=True' ]
    cmd.extend(self.getParams(appendDatasetFiles=False))
    file_list = [ file for file in self.getGridJobs()[job_id] if file not in self.ignoreFiles ]
    if len(file_list) > 0:
      file_list = ','.join(file_list)
      cmd.append(f'inputFiles={file_list}')
      ps_call(cmd, cwd=job_home, env=self.getCmsswEnv(), singularity_cmd=self.singularity_cmd)
      _, scriptName = os.path.split(self.scriptExe)
      ps_call([ os.path.join(job_home, scriptName) ], shell=True, cwd=job_home, env=self.getCmsswEnv(),
              singularity_cmd=self.singularity_cmd)
    return True

  def kill(self):
    if self.isInLocalRunMode():
      print(f'{self.name}: cannot kill a task with local jobs.')
    else:
      ps_call(['crab', 'kill', '-d', self.crabArea()], timeout=Task.crabOperationTimeout, env=self.getCmsswEnv(),
              singularity_cmd=self.singularity_cmd)

  def getProcessedFiles(self, useCacheOnly=False):
    cache_file = os.path.join(self.workArea, 'processed_files.json')
    has_changes = False
    if self.processedFilesCache is None:
      if os.path.exists(cache_file):
        with open(cache_file, 'r') as f:
          self.processedFilesCache = json.load(f)
      else:
        self.processedFilesCache = {}
        has_changes = True

    ls_result = {}

    def collectOutputs(fileId):
      filePaths = {}
      for output in self.getOutputs():
        fileName = f'{output["name"]}_{fileId}{output["ext"]}'
        filePath = os.path.join(output['crabOutput'], fileName)
        if output["file"] not in ls_result:
          ls_result[output["file"]] = {}
          ls_files = gfal_ls_safe(output['crabOutput'], voms_token=self.getVomsToken(), verbose=0)
          if ls_files is not None:
            for file_info in ls_files:
              ls_result[output["file"]][file_info.name] = file_info
        if fileName not in ls_result[output["file"]]:
          return None
        filePaths[output['file']] = filePath
      return filePaths

    if not useCacheOnly:
      for file, fileId in self.getDatasetFiles().items():
        if fileId in self.processedFilesCache: continue
        outputPaths = collectOutputs(fileId)
        if outputPaths is not None:
          self.processedFilesCache[file] = {
            'id': fileId,
            'outputs': outputPaths
          }
          has_changes = True

    if has_changes:
      with open(cache_file, 'w') as f:
        json.dump(self.processedFilesCache, f, indent=2)
    return self.processedFilesCache

  def getFilesStats(self, useCacheOnly=True):
    allFiles = set(self.getDatasetFiles().keys())
    processedFiles = set(self.getProcessedFiles(useCacheOnly=useCacheOnly).keys())
    toProcess = allFiles - processedFiles - set(self.ignoreFiles)
    return len(allFiles), len(processedFiles), len(toProcess), len(self.ignoreFiles)

  def getFilesToProcess(self):
    allFiles = set(self.getDatasetFiles().keys())
    processedFiles = set(self.getProcessedFiles().keys())
    return list(allFiles - processedFiles - set(self.ignoreFiles))

  def checkCompleteness(self):
    filesToProcess = self.getFilesToProcess()
    if len(filesToProcess) > 0:
      print(f'{self.name}: task is not complete. The following files still needs to be processed: {filesToProcess}')
      return False
    return True

  def updateConfig(self, mainCfg, taskCfg):
    taskName = self.name
    customTask = type(taskCfg[taskName]) == dict
    for pName in Task._taskCfgProperties:
      self._setFromCfg(pName, mainCfg, add=False)
      if 'config' in taskCfg:
        self._setFromCfg(pName, taskCfg['config'], add=True)
      if customTask:
        self._setFromCfg(pName, taskCfg[taskName], add=True)
    if customTask:
      inputDataset = taskCfg[taskName]['inputDataset']
    else:
      inputDataset = taskCfg[taskName]
    if inputDataset != self.inputDataset:
      raise RuntimeError(f'{self.name}: change of input dataset is not possible')
    for job_id in self.getGridJobs():
      job_id = str(job_id)
      if job_id in self.taskStatus.details and self.taskStatus.details[job_id]["State"] == "failed":
        job_flag_file = self.getGridJobDoneFlagFile(job_id)
        if os.path.exists(job_flag_file):
          os.remove(job_flag_file)
    self.getOutputs(forceUpdate=True)
    self.saveCfg()
    if self.taskStatus.status == Status.Failed:
      self.taskStatus.status = Status.WaitingForRecovery
      self.saveStatus()

  def updateStatusFromFile(self, statusPath=None, not_exists_ok=True):
    if statusPath is None:
      statusPath = self.statusPath
    if os.path.exists(statusPath):
      with open(statusPath, 'r') as f:
        self.taskStatus = CrabTaskStatus.from_json(f.read())
      return True
    if not not_exists_ok:
      raise RuntimeError(f'{self.name}: Unable to update config from "{statusPath}".')

  @staticmethod
  def Load(workArea=None, mainWorkArea=None, taskName=None):
    task = Task()
    if (workArea is not None and (mainWorkArea is not None or taskName is not None)) \
        or (mainWorkArea is not None and taskName is None) or (workArea is None and mainWorkArea is None):
      raise RuntimeError("ambiguous Task.Load params")
    if workArea is not None:
      task.workArea = workArea
    else:
      task.name = taskName
      task.workArea = os.path.join(mainWorkArea, taskName)
    task.cfgPath = os.path.join(task.workArea, 'cfg.json')
    task.statusPath = os.path.join(task.workArea, 'status.json')
    with open(task.cfgPath, 'r') as f:
      cfg = json.load(f)
    for pName in Task._taskCfgPrivateProperties + Task._taskCfgProperties:
      task._setFromCfg(pName, cfg, add=False)
    task.updateStatusFromFile()
    return task

  @staticmethod
  def Create(mainWorkArea, mainCfg, taskCfg, taskName):
    task = Task()
    task.startDate = timestamp_str()
    task.workArea = os.path.join(mainWorkArea, taskName)
    task.cfgPath = os.path.join(task.workArea, 'cfg.json')
    task.statusPath = os.path.join(task.workArea, 'status.json')
    if os.path.exists(task.workArea):
      raise RuntimeError(f'Task with name "{taskName}" already exists.')
    os.mkdir(task.workArea)
    customTask = type(taskCfg[taskName]) == dict
    for pName in Task._taskCfgProperties:
      task._setFromCfg(pName, mainCfg, add=False)
      if 'config' in taskCfg:
        task._setFromCfg(pName, taskCfg['config'], add=True)
      if customTask:
        task._setFromCfg(pName, taskCfg[taskName], add=True)
    task.taskStatus.status = Status.Defined
    if customTask:
      task.inputDataset = taskCfg[taskName]['inputDataset']
    else:
      task.inputDataset = taskCfg[taskName]
    task.name = taskName
    task.getOutputs(forceUpdate=True)
    task.saveCfg()
    task.saveStatus()
    return task

if __name__ == "__main__":
  import sys

  workArea = sys.argv[1]
  task = Task.Load(workArea=workArea)

  # ok = "OK" if task.checkCompleteness(includeNotFinishedFromLastIteration=False) else "INCOMPLETE"
  # print(f'{task.name}: {ok}')
  # print(task.getAllOutputPaths())
  filesToProcess = task.getFilesToProcess()
  print(f'{task.name}: {len(filesToProcess)} {filesToProcess}')
  lumiMask = task.getRepresentativeLumiMask(filesToProcess)
  n_lumi = sum([ len(x) for _, x in lumiMask.items()])
  print(f'{task.name}: {n_lumi} {lumiMask}')
