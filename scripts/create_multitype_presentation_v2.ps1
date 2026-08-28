param([string]$OutputDir = "output\multitype-corpus")

$ErrorActionPreference = "Stop"
$root = (Resolve-Path ".").Path
$absoluteOutput = Join-Path $root $OutputDir
$renderDir = Join-Path $absoluteOutput "rendered-pptx"
New-Item -ItemType Directory -Force -Path $absoluteOutput, $renderDir | Out-Null
$pptxPath = Join-Path $absoluteOutput "recovery-drill-brief.pptx"

function Set-TextStyle($shape, [string]$text, [int]$fontSize, [bool]$bold, [int]$color) {
    $shape.TextFrame.TextRange.Text = $text
    $shape.TextFrame.TextRange.Font.Name = "Aptos"
    $shape.TextFrame.TextRange.Font.Size = $fontSize
    $shape.TextFrame.TextRange.Font.Bold = if ($bold) { -1 } else { 0 }
    $shape.TextFrame.TextRange.Font.Color.RGB = $color
    $shape.TextFrame.MarginLeft = 0
    $shape.TextFrame.MarginRight = 0
    $shape.TextFrame.MarginTop = 0
    $shape.TextFrame.MarginBottom = 0
}

$ppt = New-Object -ComObject PowerPoint.Application
try {
    $deck = $ppt.Presentations.Add($false)
    $deck.PageSetup.SlideWidth = 960
    $deck.PageSetup.SlideHeight = 540

    $slide = $deck.Slides.Add(1, 12)
    $slide.FollowMasterBackground = 0
    $slide.Background.Fill.ForeColor.RGB = 16448250
    $accent = $slide.Shapes.AddShape(1, 64, 68, 10, 324)
    $accent.Fill.ForeColor.RGB = 14120960
    $accent.Line.Visible = 0
    $eyebrow = $slide.Shapes.AddTextbox(1, 98, 76, 680, 28)
    Set-TextStyle $eyebrow "DISASTER RECOVERY / INTERNAL" 14 $true 7566195
    $title = $slide.Shapes.AddTextbox(1, 98, 142, 720, 150)
    Set-TextStyle $title "Retrieval Gateway Recovery Drill" 40 $true 1973790
    $subtitle = $slide.Shapes.AddTextbox(1, 98, 316, 700, 72)
    Set-TextStyle $subtitle "Drill PPTX-7319 validates real presentation parsing, layout semantics, and retrieval." 20 $false 5263440

    $slide = $deck.Slides.Add(2, 12)
    $slide.FollowMasterBackground = 0
    $slide.Background.Fill.ForeColor.RGB = 16777215
    $title = $slide.Shapes.AddTextbox(1, 64, 48, 830, 82)
    Set-TextStyle $title "Restore the previous index generation within 23 minutes" 30 $true 1973790
    $line = $slide.Shapes.AddShape(1, 110, 257, 720, 4)
    $line.Fill.ForeColor.RGB = 13158600
    $line.Line.Visible = 0
    $labels = @(
        @("0 min", "Trigger rollback", 118),
        @("8 min", "Switch alias", 348),
        @("23 min", "Recovery deadline", 648)
    )
    foreach ($item in $labels) {
        $dot = $slide.Shapes.AddShape(9, [int]$item[2], 240, 38, 38)
        $dot.Fill.ForeColor.RGB = if ($item[0] -eq "23 min") { 2138521 } else { 14120960 }
        $dot.Line.Visible = 0
        $time = $slide.Shapes.AddTextbox(1, [int]$item[2] - 30, 196, 120, 30)
        Set-TextStyle $time $item[0] 17 $true 1973790
        $caption = $slide.Shapes.AddTextbox(1, [int]$item[2] - 55, 300, 180, 48)
        Set-TextStyle $caption $item[1] 16 $false 5263440
    }
    $note = $slide.Shapes.AddTextbox(1, 110, 410, 720, 44)
    Set-TextStyle $note "Acceptance fact: recovery limit = 23 minutes; owner = Retrieval Platform Lead." 19 $true 2138521

    $slide = $deck.Slides.Add(3, 12)
    $slide.FollowMasterBackground = 0
    $slide.Background.Fill.ForeColor.RGB = 1973790
    $eyebrow = $slide.Shapes.AddTextbox(1, 72, 64, 500, 28)
    Set-TextStyle $eyebrow "VALIDATION QUERY" 14 $true 11053224
    $question = $slide.Shapes.AddTextbox(1, 72, 128, 790, 110)
    Set-TextStyle $question "How quickly must PPTX-7319 restore the previous index generation?" 31 $true 16777215
    $answerBox = $slide.Shapes.AddShape(5, 72, 286, 330, 112)
    $answerBox.Fill.ForeColor.RGB = 14120960
    $answerBox.Line.Visible = 0
    $answer = $slide.Shapes.AddTextbox(1, 104, 316, 270, 52)
    Set-TextStyle $answer "23 minutes" 34 $true 16777215
    $owner = $slide.Shapes.AddTextbox(1, 456, 306, 350, 72)
    Set-TextStyle $owner "Owner`rRetrieval Platform Lead" 20 $false 13882323

    $deck.SaveAs($pptxPath, 24)
    $deck.Export($renderDir, "PNG", 1280, 720)
    $deck.Close()
}
finally {
    $ppt.Quit()
}

Get-Item $pptxPath | Select-Object FullName, Length
Get-ChildItem $renderDir -Filter "*.PNG" | Select-Object Name, Length
