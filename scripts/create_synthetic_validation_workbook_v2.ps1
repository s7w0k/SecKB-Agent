param([string]$OutputPath = "output\multitype-corpus\mindbridge-risk-ledger.xlsx")

$ErrorActionPreference = "Stop"
$absolutePath = Join-Path (Resolve-Path ".").Path $OutputPath
$excel = New-Object -ComObject Excel.Application
$excel.Visible = $false
$excel.DisplayAlerts = $false
try {
    $workbook = $excel.Workbooks.Add()
    $sheet = $workbook.Worksheets.Item(1)
    $sheet.Name = "Recovery Drill"
    $values = [object[,]]::new(4, 7)
    $values[0,0] = "reportId";  $values[0,1] = "riskLevel"; $values[0,2] = "category"
    $values[0,3] = "confidence"; $values[0,4] = "summary"; $values[0,5] = "createdAt"; $values[0,6] = "action"
    $values[1,0] = 1; $values[1,1] = "MEDIUM"; $values[1,2] = "LOAD_TEST"; $values[1,3] = 0.85
    $values[1,4] = "Synthetic baseline load test"; $values[1,5] = "2026-08-28T09:00:00"
    $values[2,0] = 2; $values[2,1] = "HIGH"; $values[2,2] = "RECOVERY_DRILL"; $values[2,3] = 0.95
    $values[2,4] = "Synthetic index recovery escalation"; $values[2,5] = "2026-08-28T09:05:00"
    $values[3,0] = 3; $values[3,1] = "LOW"; $values[3,2] = "NORMAL"; $values[3,3] = 0.95
    $values[3,4] = "Synthetic steady-state check"; $values[3,5] = "2026-08-28T09:10:00"
    $sheet.Range("A1:G4").Value2 = $values
    $sheet.Range("G2").Formula = '=IF(B2="HIGH","ESCALATE","MONITOR")'
    $sheet.Range("G2:G4").FillDown()
    $header = $sheet.Range("A1:G1")
    $header.Font.Bold = $true
    $header.Font.Color = 16777215
    $header.Interior.Color = 10040064
    $header.RowHeight = 24
    $sheet.Range("A1:G4").AutoFilter() | Out-Null
    $sheet.Range("A1:G4").Font.Name = "Aptos"
    $sheet.Range("D2:D4").NumberFormat = "0.00"
    $widths = @(11, 13, 20, 12, 38, 22, 14)
    for ($index = 1; $index -le 7; $index++) { $sheet.Columns.Item($index).ColumnWidth = $widths[$index - 1] }
    $sheet.Application.ActiveWindow.SplitRow = 1
    $sheet.Application.ActiveWindow.FreezePanes = $true
    $workbook.SaveAs($absolutePath, 51)
    $workbook.Close($false)
}
finally {
    $excel.Quit()
}
Get-Item $absolutePath | Select-Object FullName, Length
