function FailUserProfileResolution: String;
begin
  RaiseException('OSIA installer could not resolve the current user profile.');
  Result := '';
end;

function ResolveUserProfileDirectory: String;
var
  Candidate: String;
  Drive: String;
  SourceDirectory: String;
begin
  Candidate := Trim(GetEnv('USERPROFILE'));
  if (Length(Candidate) >= 2) and (Candidate[1] = '"') and
    (Candidate[Length(Candidate)] = '"') then
    Candidate := Trim(Copy(Candidate, 2, Length(Candidate) - 2));

  if (Candidate = '') or (Pos('{', Candidate) > 0) or
    (Pos('}', Candidate) > 0) then
  begin
    Result := FailUserProfileResolution;
    exit;
  end;

  Candidate := PathNormalizeSlashes(Candidate);
  if PathHasInvalidCharacters(Candidate, True) or
    (not PathIsRooted(Candidate)) then
  begin
    Result := FailUserProfileResolution;
    exit;
  end;

  Drive := ExtractFileDrive(Candidate);
  if (Length(Drive) <> 2) or (Drive[2] <> ':') then
  begin
    Result := FailUserProfileResolution;
    exit;
  end;

  Candidate := RemoveBackslashUnlessRoot(ExpandFileName(Candidate));
  SourceDirectory := RemoveBackslashUnlessRoot(
    ExpandFileName(PathNormalizeSlashes(ExpandConstant('{src}'))));
  if PathSame(Candidate, SourceDirectory) or
    PathStartsWith(Candidate, AddBackslash(SourceDirectory), True) then
  begin
    Result := FailUserProfileResolution;
    exit;
  end;

  Result := Candidate;
end;

function ResolveOsiaProfileDirectory: String;
begin
  Result := AddBackslash(ResolveUserProfileDirectory) + '.osia';
end;
