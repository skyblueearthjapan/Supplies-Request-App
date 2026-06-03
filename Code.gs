function doGet(e) {
  var template = HtmlService.createTemplateFromFile('Index');
  template.initialRequestId = e && e.parameter ? e.parameter.requestId || '' : '';
  return template
    .evaluate()
    .setTitle(APP.NAME)
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}

function include(filename) {
  return HtmlService.createHtmlOutputFromFile(filename).getContent();
}

function setupApplication() {
  var spreadsheet = getSpreadsheet_();
  var folder = getPdfFolder_();

  var workerCount = 0;
  try {
    ensureMasterSyncTrigger_();
    var syncResult = syncWorkersMaster();
    workerCount = (syncResult && syncResult.count) || 0;
  } catch (error) {
    Logger.log('setupApplication: master sync skipped: ' + error.message);
  }

  return {
    spreadsheetId: spreadsheet.getId(),
    spreadsheetUrl: spreadsheet.getUrl(),
    pdfFolderId: folder.getId(),
    pdfFolderUrl: folder.getUrl(),
    workerCount: workerCount
  };
}
